// gnome-uebersicht — GNOME Shell extension.
//
// Launches the daemon (uebersicht_daemon.py) as a trusted Wayland client and
// turns its transparent window into a desktop layer: DESKTOP window type (below
// all windows, hidden from alt-tab/dock/overview), on all workspaces, covering
// the primary monitor. Also adds a menu to the top bar.
//
// All widget logic lives in the daemon and the JS runtime — changing those only
// needs "Restart daemon" from the menu, not a logout.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const RESTART_DELAYS_S = [1, 2, 5, 10, 30, 60];

export default class UebersichtExtension extends Extension {
    enable() {
        this._settingsHidden = false;
        this._restartCount = 0;
        this._restartTimer = 0;
        this._placeTimer = 0;
        this._window = null;
        this._windowSignals = [];

        this._buildIndicator();

        this._mapId = global.window_manager.connect_after('map', (_wm, actor) =>
            this._maybeAdopt(actor.get_meta_window()));
        this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._place());
        this._workAreaId = global.display.connect('workareas-changed', () => this._place());

        this._launch();
    }

    disable() {
        for (const [obj, id] of [
            [global.window_manager, this._mapId],
            [Main.layoutManager, this._monitorsId],
            [global.display, this._workAreaId],
        ])
            if (id) obj.disconnect(id);
        this._mapId = this._monitorsId = this._workAreaId = 0;

        if (this._restartTimer)
            GLib.source_remove(this._restartTimer);
        if (this._placeTimer)
            GLib.source_remove(this._placeTimer);
        this._restartTimer = this._placeTimer = 0;

        this._releaseWindow();
        this._stopping = true;
        this._kill();
        this._stopping = false;

        this._indicator?.destroy();
        this._indicator = null;
    }

    // ------------------------------------------------------------ daemon process
    _launch() {
        const daemon = GLib.build_filenamev([this.path, 'uebersicht_daemon.py']);
        const argv = ['/usr/bin/env', 'python3', '-u', daemon, '--stdin-control'];
        const launcher = new Gio.SubprocessLauncher({
            flags: Gio.SubprocessFlags.STDIN_PIPE |
                   Gio.SubprocessFlags.STDOUT_PIPE |
                   Gio.SubprocessFlags.STDERR_MERGE,
        });
        try {
            this._client = Meta.WaylandClient.new_subprocess(global.context, launcher, argv);
            this._proc = this._client.get_subprocess();
        } catch (e) {
            console.error(`[uebersicht] cannot launch the daemon: ${e.message}`);
            this._client = this._proc = null;
            this._scheduleRestart();
            return;
        }
        launcher.close?.();

        this._stdin = this._proc.get_stdin_pipe();
        this._readLog(Gio.DataInputStream.new(this._proc.get_stdout_pipe()));
        const proc = this._proc;
        proc.wait_async(null, () => {
            if (this._proc !== proc)
                return;
            console.log(`[uebersicht] daemon exited (status ${proc.get_exit_status?.()})`);
            this._proc = this._client = this._stdin = null;
            this._releaseWindow();
            if (!this._stopping)
                this._scheduleRestart();
        });
    }

    _readLog(stream) {
        stream.read_line_async(GLib.PRIORITY_LOW, null, (s, res) => {
            let line;
            try {
                [line] = s.read_line_finish_utf8(res);
            } catch {
                return;
            }
            if (line === null)
                return;
            if (line.startsWith('@@ubs '))
                this._onStatus(line.slice(6));
            else
                console.log(`[uebersicht] ${line}`);
            this._readLog(s);
        });
    }

    _scheduleRestart() {
        if (this._restartTimer)
            return;
        const delay = RESTART_DELAYS_S[Math.min(this._restartCount, RESTART_DELAYS_S.length - 1)];
        this._restartCount++;
        this._restartTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, delay, () => {
            this._restartTimer = 0;
            this._launch();
            return GLib.SOURCE_REMOVE;
        });
    }

    _kill() {
        if (!this._proc)
            return;
        const proc = this._proc;
        this._proc = this._client = null;
        try {
            this._stdin?.close(null);
        } catch {
            // already closed by the dying process
        }
        this._stdin = null;
        proc.force_exit();
    }

    _restartDaemon() {
        this._restartCount = 0;
        this._stopping = true;
        this._releaseWindow();
        this._kill();
        this._stopping = false;
        this._launch();
    }

    _send(cmd) {
        if (!this._stdin)
            return;
        const bytes = new TextEncoder().encode(`${cmd}\n`);
        this._stdin.write_bytes_async(new GLib.Bytes(bytes), GLib.PRIORITY_DEFAULT, null, (s, res) => {
            try {
                s.write_bytes_finish(res);
            } catch (e) {
                console.error(`[uebersicht] stdin: ${e.message}`);
            }
        });
    }

    // ------------------------------------------------------------ desktop window
    _maybeAdopt(win) {
        if (!win || !this._client)
            return;
        let ours = false;
        try {
            ours = this._client.owns_window(win);
        } catch {
            // owns_window() throws for X11 windows — those are never ours
        }
        if (!ours)
            return;
        this._releaseWindow();
        this._window = win;
        this._restartCount = 0;

        win.set_type?.(Meta.WindowType.DESKTOP);
        win.stick();
        // The DESKTOP type keeps the window in the lowest layer (below normal
        // windows). Within that layer we raise above the desktop icons (DING),
        // which lower themselves — so widgets stay clickable, while the input
        // region set by the daemon lets all other clicks through.
        win.raise();
        this._windowSignals = [
            win.connect('notify::minimized', () => win.minimized && win.unminimize()),
            win.connect('notify::above', () => win.above && win.unmake_above()),
            win.connect('position-changed', () => this._queuePlace()),
            win.connect('size-changed', () => this._queuePlace()),
            win.connect('unmanaged', () => this._releaseWindow()),
        ];
        this._place();
        console.log(`[uebersicht] adopted desktop window (type ${win.get_window_type()}, sticky ${win.is_on_all_workspaces()})`);
    }

    _releaseWindow() {
        if (this._window) {
            for (const id of this._windowSignals)
                this._window.disconnect(id);
        }
        this._windowSignals = [];
        this._window = null;
    }

    _queuePlace() {
        if (this._placeTimer)
            return;
        this._placeTimer = GLib.timeout_add(GLib.PRIORITY_LOW, 200, () => {
            this._placeTimer = 0;
            this._place();
            return GLib.SOURCE_REMOVE;
        });
    }

    _place() {
        const win = this._window;
        const mon = Main.layoutManager.primaryMonitor;
        if (!win || !mon)
            return;
        const r = win.get_frame_rect();
        if (r.x !== mon.x || r.y !== mon.y || r.width !== mon.width || r.height !== mon.height)
            win.move_resize_frame(false, mon.x, mon.y, mon.width, mon.height);
    }

    // ------------------------------------------------------------ top bar menu
    _buildIndicator() {
        this._indicator = new PanelMenu.Button(0.0, this.metadata.name, false);
        this._indicator.add_child(new St.Icon({
            icon_name: 'view-grid-symbolic',
            style_class: 'system-status-icon',
        }));
        const menu = this._indicator.menu;

        this._visibleItem = new PopupMenu.PopupSwitchMenuItem('Widgets on desktop', true);
        this._visibleItem.connect('toggled', (_i, on) => this._send(on ? 'show' : 'hide'));
        menu.addMenuItem(this._visibleItem);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // One switch per widget, like the original Übersicht menu; filled by _onStatus().
        this._widgetsSection = new PopupMenu.PopupMenuSection();
        menu.addMenuItem(this._widgetsSection);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        menu.addAction('Refresh widgets', () => this._send('refresh'));
        menu.addAction('Reload widgets', () => this._send('reload'));
        menu.addAction('Open widgets folder', () => this._openWidgetsDir());
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        menu.addAction('Web inspector (debug)', () => this._send('inspect'));
        menu.addAction('Restart daemon', () => this._restartDaemon());

        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    _onStatus(json) {
        let status;
        try {
            status = JSON.parse(json);
        } catch (e) {
            console.error(`[uebersicht] bad status line: ${e.message}`);
            return;
        }
        if (typeof status.desktopHidden === 'boolean')
            this._visibleItem?.setToggleState(!status.desktopHidden);
        if (Array.isArray(status.widgets))
            this._fillWidgets(status.widgets);
    }

    _fillWidgets(widgets) {
        const section = this._widgetsSection;
        if (!section)
            return;
        section.removeAll();
        if (widgets.length === 0) {
            const empty = new PopupMenu.PopupMenuItem('No widgets', {reactive: false});
            section.addMenuItem(empty);
            return;
        }
        for (const w of widgets) {
            const label = w.id.replace(/\/index\.jsx?$/, '').replace(/\.jsx?$/, '');
            const item = new PopupMenu.PopupSwitchMenuItem(label, !w.hidden);
            item.connect('toggled', (_i, on) => this._send(`${on ? 'widget-show' : 'widget-hide'} ${w.id}`));
            section.addMenuItem(item);
        }
    }

    _openWidgetsDir() {
        const dir = GLib.getenv('UBERSICHT_WIDGETS_DIR') ||
            GLib.build_filenamev([GLib.get_user_data_dir(), 'uebersicht', 'widgets']);
        GLib.mkdir_with_parents(dir, 0o755);
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(dir).get_uri(), null);
    }
}
