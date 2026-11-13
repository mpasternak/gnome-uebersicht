#!/usr/bin/env python3
"""gnome-uebersicht daemon: a transparent WebKitGTK window that renders
Übersicht-style widgets.

It is launched by the GNOME Shell extension (via Meta.WaylandClient), which then
pins the window to the desktop (DESKTOP type, all workspaces, below all windows).
It can also be started by hand for testing:  ./uebersicht_daemon.py --debug

Control from the extension: lines on stdin —
  reload | refresh | hide | show | inspect | quit | widget-hide <id> | widget-show <id>
  | zoom system|<factor>
Status for the extension: lines on stdout prefixed with "@@ubs " followed by JSON.
"""

import argparse
import ctypes
import json
import mimetypes
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("JavaScriptCore", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, JavaScriptCore, WebKit2  # noqa: E402
from ubs_core import (  # noqa: E402
    HERE,
    SKIP_DIRS,
    Widgets,
    default_widgets_dir,
    esbuild_argv,
    find_esbuild,
    load_state,
    parse_zoom_mode,
    save_state,
    scale_rects,
    webkit_zoom_level,
)

RUNTIME_DIR = HERE / "runtime"
SCHEME = "ubersicht"
TITLE = "gnome-uebersicht"
RUN_TIMEOUT_S = 120


# --- input region via ctypes (avoids depending on python3-gi-cairo) ---------------
class _RectInt(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int)]


_cairo = ctypes.CDLL("libcairo.so.2")
_cairo.cairo_region_create_rectangles.restype = ctypes.c_void_p
_cairo.cairo_region_create_rectangles.argtypes = [ctypes.POINTER(_RectInt), ctypes.c_int]
_cairo.cairo_region_destroy.argtypes = [ctypes.c_void_p]
_gdk = ctypes.CDLL("libgdk-3.so.0")
_gdk.gdk_window_input_shape_combine_region.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]


def set_input_region(gdk_window, rects):
    """Clicks reach the window only inside these rectangles; everything else passes through."""
    ptr = ctypes.pythonapi.PyCapsule_GetPointer(gdk_window.__gpointer__, None)
    arr = (_RectInt * max(len(rects), 1))(*[_RectInt(*map(int, r)) for r in rects])
    region = _cairo.cairo_region_create_rectangles(arr, len(rects))
    _gdk.gdk_window_input_shape_combine_region(ptr, region, 0, 0)
    _cairo.cairo_region_destroy(region)


def log(*a):
    print("[ubs]", *a, file=sys.stderr, flush=True)


class Daemon:
    def __init__(self, args):
        self.args = args
        self.widgets = Widgets(Path(args.widgets).expanduser())
        self.widgets.root.mkdir(parents=True, exist_ok=True)
        self.esbuild = find_esbuild(args.esbuild)
        if not self.esbuild:
            log("WARNING: esbuild not found — JSX widgets will not compile")
        self.monitors: dict[str, Gio.FileMonitor] = {}
        self.reload_timer = 0
        self.pending_changes: set[str] = set()
        self.hidden = bool(load_state().get("desktop_hidden", False))
        self.hidden_widgets: set[str] = set(load_state().get("hidden_widgets", []))
        self.zoom_mode = parse_zoom_mode(load_state().get("zoom"))
        self.implicit_zoom = 1.0  # zoom WebKit applies by itself, measured from the viewport
        self.zoom_settle_until = 0  # ignore viewport reports from before the last zoom change
        self.iface = Gio.Settings.new("org.gnome.desktop.interface")
        self.iface.connect("changed::text-scaling-factor", lambda *_: self._apply_zoom())

        self._build_window()
        self._watch_tree()
        self._watch_stdin()
        self._announce()

    # ---------------------------------------------------------------- window
    def _build_window(self):
        data_dir = Path(GLib.get_user_data_dir()) / "gnome-uebersicht"
        cache_dir = Path(GLib.get_user_cache_dir()) / "gnome-uebersicht"
        mgr = WebKit2.WebsiteDataManager(base_data_directory=str(data_dir), base_cache_directory=str(cache_dir))
        ctx = WebKit2.WebContext.new_with_website_data_manager(mgr)
        ctx.register_uri_scheme(SCHEME, self._on_scheme)
        sec = ctx.get_security_manager()
        sec.register_uri_scheme_as_secure(SCHEME)
        sec.register_uri_scheme_as_cors_enabled(SCHEME)

        ucm = WebKit2.UserContentManager()
        ucm.connect("script-message-with-reply-received::ubersicht", self._on_message)
        ucm.register_script_message_handler_with_reply("ubersicht", None)

        self.view = WebKit2.WebView(web_context=ctx, user_content_manager=ucm)
        s = self.view.get_settings()
        s.set_enable_developer_extras(True)
        s.set_enable_write_console_messages_to_stdout(True)
        s.set_javascript_can_access_clipboard(True)
        self.view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        self.view.connect("decide-policy", self._on_policy)
        self.view.connect("web-process-terminated", lambda v, r: (log("web process terminated:", r), v.reload()))
        self.view.connect("context-menu", lambda *a: not self.args.debug)  # no context menu unless debugging

        self.win = Gtk.Window(title=TITLE)
        self.win.set_decorated(self.args.debug)
        self.win.set_app_paintable(True)
        visual = self.win.get_screen().get_rgba_visual()
        if visual:
            self.win.set_visual(visual)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_below(True)
        geo = self._monitor_geometry()
        self.win.set_default_size(geo.width, geo.height)
        self.win.add(self.view)
        self.win.connect("destroy", lambda *_: Gtk.main_quit())
        self.win.connect("realize", lambda *_: self._set_region([]))
        self.win.show_all()

        self.view.load_uri(f"{SCHEME}://app/")

    def _show_inspector(self):
        inspector = self.view.get_inspector()
        if not getattr(self, "_inspector_detached", False):
            # Docking would put the inspector inside the desktop window: below all
            # windows and outside the click-through input region. Keep it separate.
            inspector.connect("attach", lambda *_: True)
            self._inspector_detached = True
        inspector.show()
        inspector.detach()

    def _monitor_geometry(self):
        disp = Gdk.Display.get_default()
        mon = disp.get_primary_monitor() or disp.get_monitor(0)
        return mon.get_geometry()

    def _on_policy(self, view, decision, kind):
        # Links clicked in widgets open in the default browser, not in the desktop window.
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(f"{SCHEME}:") and not uri.startswith("about:"):
                try:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                except GLib.Error as e:
                    log("cannot open", uri, e.message)
                decision.ignore()
                return True
        return False

    def _set_region(self, rects):
        if self.args.debug:
            return
        gw = self.win.get_window()
        if gw is not None:
            set_input_region(gw, rects)

    # ---------------------------------------------------------------- files
    def _on_scheme(self, req):
        path = req.get_path() or "/"
        if path in ("/", "/index.html"):
            f = RUNTIME_DIR / "index.html"
        elif path.startswith("/__ubs/"):
            f = (RUNTIME_DIR / path[len("/__ubs/") :]).resolve()
            if RUNTIME_DIR.resolve() not in f.parents:
                f = None
        else:
            f = self.widgets.path(GLib.uri_unescape_string(path.lstrip("/"), None) or "")
        if not f or not f.is_file():
            req.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), "not found", Gio.IOErrorEnum.NOT_FOUND))
            return
        data = f.read_bytes()
        mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        req.finish(stream, len(data), mime)

    def _watch_tree(self):
        root = self.widgets.root
        wanted = set()
        for dirpath, dirnames, _ in os.walk(root, followlinks=True):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            wanted.add(dirpath)
        for d in list(self.monitors):
            if d not in wanted:
                self.monitors.pop(d).cancel()
        for d in wanted - set(self.monitors):
            m = Gio.File.new_for_path(d).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            m.connect("changed", self._on_fs_change)
            self.monitors[d] = m

    def _on_fs_change(self, mon, f, other, event):
        if event not in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.RENAMED,
        ):
            return
        p = f.get_path() or ""
        if "/node_modules/" in p or os.path.basename(p).startswith("."):
            return
        try:
            self.pending_changes.add(str(Path(p).relative_to(self.widgets.root)))
        except ValueError:
            pass
        if self.reload_timer:
            GLib.source_remove(self.reload_timer)
        self.reload_timer = GLib.timeout_add(300, self._flush_reload)

    def _flush_reload(self):
        self.reload_timer = 0
        changed, self.pending_changes = self.pending_changes, set()
        self._watch_tree()
        ids = set(self.widgets.ids())
        # only widget files changed → reload just those; a helper file changed → reload all
        only = sorted(changed & ids) if changed and changed <= ids else None
        log("reloading:", only or "all widgets")
        self.js(f"window.__ubs && window.__ubs.reload({json.dumps(only)})")
        self._announce()
        return GLib.SOURCE_REMOVE

    def _apply_zoom(self):
        text_scale = self.iface.get_double("text-scaling-factor")
        level = webkit_zoom_level(self.zoom_mode, text_scale, self.implicit_zoom)
        if abs(level - self.view.get_zoom_level()) > 0.005:
            log(f"zoom: mode={self.zoom_mode} text={text_scale:.2f} implicit={self.implicit_zoom:.3f} → {level:.3f}")
            self.view.set_zoom_level(level)  # the page resizes and resends its region
            self.zoom_settle_until = GLib.get_monotonic_time() + 500_000
            # WebKit may also react to the same setting by itself; re-measure once settled
            GLib.timeout_add(600, lambda: self.js("window.__ubs && window.__ubs.resendRegion()") and False)

    def _set_zoom_mode(self, value):
        self.zoom_mode = parse_zoom_mode(value)
        state = load_state()
        state["zoom"] = self.zoom_mode
        save_state(state)
        self._apply_zoom()
        self._announce()

    def _apply_hidden(self):
        self.js(f"window.__ubs && window.__ubs.setHidden({json.dumps(self.hidden)})")

    def visible_ids(self) -> list[str]:
        return self.widgets.visible(self.hidden_widgets)

    def _announce(self):
        """Tell the extension which widgets exist and which are hidden."""
        widgets = [{"id": i, "hidden": i in self.hidden_widgets} for i in self.widgets.ids()]
        status = {"widgets": widgets, "desktopHidden": self.hidden, "zoom": self.zoom_mode}
        print("@@ubs " + json.dumps(status), flush=True)

    def _set_widget_hidden(self, wid: str, hidden: bool):
        if hidden:
            self.hidden_widgets.add(wid)
        else:
            self.hidden_widgets.discard(wid)
        state = load_state()
        state["hidden_widgets"] = sorted(self.hidden_widgets)
        save_state(state)
        # sync([]) mounts newly visible widgets and drops hidden ones, leaving the rest alone
        self.js("window.__ubs && window.__ubs.reload([])")
        self._announce()

    def js(self, code):
        self.view.evaluate_javascript(code, -1, None, None, None, None, None)

    # ---------------------------------------------------------------- JS bridge
    def _on_message(self, ucm, value, reply):
        try:
            msg = json.loads(value.to_json(0))
        except Exception as e:  # noqa: BLE001
            self._reply(reply, value, {"error": f"bad message: {e}"})
            return True
        op = msg.get("op")
        if op == "run":
            self._run(msg.get("cmd", ""), lambda r: self._reply(reply, value, r))
        elif op == "compile":
            self._compile(msg.get("id", ""), lambda r: self._reply(reply, value, r))
        elif op == "list":
            self._reply(reply, value, {"ids": self.visible_ids(), "hidden": self.hidden})
        elif op == "region":
            alloc = (self.view.get_allocated_width(), self.view.get_allocated_height())
            viewport = msg.get("viewport")
            settled = GLib.get_monotonic_time() >= self.zoom_settle_until
            if settled and viewport and viewport[0] > 0 and alloc[0] > 0:
                self.implicit_zoom = alloc[0] / (viewport[0] * self.view.get_zoom_level())
                self._apply_zoom()
            self._set_region(scale_rects(msg.get("rects") or [], viewport, alloc))
            self._reply(reply, value, {"ok": True})
        else:
            self._reply(reply, value, {"error": f"unknown operation {op!r}"})
        return True

    def _reply(self, reply, value, obj):
        ctx = value.get_context()
        reply.return_value(JavaScriptCore.Value.new_from_json(ctx, json.dumps(obj)))

    def _spawn(self, argv, cwd, done, stdin_text=None):
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_PIPE
            | (Gio.SubprocessFlags.STDIN_PIPE if stdin_text is not None else 0)
        )
        launcher.set_cwd(str(cwd))
        local_bin = str(Path.home() / ".local" / "bin")
        path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        if local_bin not in path.split(":"):
            launcher.setenv("PATH", f"{local_bin}:{path}", True)
        try:
            proc = launcher.spawnv(argv)
        except GLib.Error as e:
            done({"status": 127, "stdout": "", "stderr": e.message})
            return
        timer = GLib.timeout_add_seconds(RUN_TIMEOUT_S, lambda: (proc.force_exit(), GLib.SOURCE_REMOVE)[1])

        def finish(p, res):
            GLib.source_remove(timer)
            try:
                _, out, err = p.communicate_finish(res)
                out = out.get_data().decode("utf-8", "replace") if out else ""
                err = err.get_data().decode("utf-8", "replace") if err else ""
                status = p.get_exit_status() if p.get_if_exited() else 128 + p.get_term_sig()
            except GLib.Error as e:
                out, err, status = "", e.message, 1
            done({"status": status, "stdout": out, "stderr": err})

        data = GLib.Bytes.new(stdin_text.encode()) if stdin_text is not None else None
        proc.communicate_async(data, None, finish)

    def _run(self, cmd, done):
        # Like Übersicht: run through a shell, with the widgets directory as cwd.
        self._spawn(["/bin/bash", "-c", cmd], self.widgets.root, done)

    def _compile(self, wid, done):
        p = self.widgets.path(wid)
        if not p:
            done({"error": f"no such widget: {wid}"})
            return
        if not self.esbuild:
            done({"error": "esbuild not found — run `npm install` in the gnome-uebersicht repository"})
            return
        argv = esbuild_argv(self.esbuild, p)

        def fin(r):
            if r["status"] == 0:
                done({"code": r["stdout"]})
            else:
                done({"error": r["stderr"] or r["stdout"] or f"esbuild exit {r['status']}"})

        self._spawn(argv, p.parent, fin)

    # ---------------------------------------------------------------- stdin
    def _watch_stdin(self):
        if not self.args.stdin_control or sys.stdin is None:
            return
        ch = GLib.IOChannel.unix_new(sys.stdin.fileno())
        GLib.io_add_watch(ch, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_stdin)

    def _on_stdin(self, ch, cond):
        if cond & GLib.IOCondition.HUP:
            # the extension went away — do not linger as an orphan
            log("stdin hung up — exiting")
            Gtk.main_quit()
            return False
        line = sys.stdin.readline()
        if not line:
            log("stdin closed — exiting")
            Gtk.main_quit()
            return False
        cmd, _, arg = line.strip().partition(" ")
        log("command:", cmd, arg)
        if cmd == "zoom" and arg:
            self._set_zoom_mode(arg)
        elif cmd in ("widget-hide", "widget-show") and arg:
            self._set_widget_hidden(arg, cmd == "widget-hide")
        elif cmd == "reload":
            self.js("window.__ubs && window.__ubs.reload(null)")
        elif cmd == "refresh":
            self.js("window.__ubs && window.__ubs.refreshAll()")
        elif cmd in ("hide", "show"):
            self.hidden = cmd == "hide"
            state = load_state()
            state["desktop_hidden"] = self.hidden
            save_state(state)
            self._apply_hidden()
        elif cmd == "inspect":
            self._show_inspector()
        elif cmd == "quit":
            Gtk.main_quit()
        return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--widgets", default=str(default_widgets_dir()), help="widgets directory")
    ap.add_argument("--esbuild", default=None, help="path to the esbuild binary")
    ap.add_argument("--debug", action="store_true", help="regular decorated window, no click-through")
    ap.add_argument("--stdin-control", action="store_true", help="read control commands from stdin (extension)")
    ap.add_argument("--dump-after", type=float, default=0, help="(testing) print page text after N s, exit")
    ap.add_argument("--dump-js", default=None, help="(testing) JS file whose result --dump-after prints")
    args = ap.parse_args()
    GLib.set_prgname("gnome-uebersicht")
    d = Daemon(args)
    log("widgets from", args.widgets)
    if args.dump_after:

        def dump():
            def got(view, res):
                try:
                    v = view.evaluate_javascript_finish(res)
                    print(v.to_string(), flush=True)
                except GLib.Error as e:
                    log("dump:", e.message)
                Gtk.main_quit()

            default_js = (
                "JSON.stringify({text: document.body.innerText,"
                " html: document.getElementById('ubs-root').innerHTML.slice(0, 600),"
                " ls: localStorage.length})"
            )
            if args.dump_js:
                # the file is an async function body: may `await`, must `return` a string
                body = Path(args.dump_js).read_text()

                def got_async(view, res):
                    try:
                        print(view.call_async_javascript_function_finish(res).to_string(), flush=True)
                    except GLib.Error as e:
                        log("dump:", e.message)
                    Gtk.main_quit()

                d.view.call_async_javascript_function(body, -1, None, None, None, None, got_async)
                return False
            d.view.evaluate_javascript(
                default_js,
                -1,
                None,
                None,
                None,
                got,
            )
            return False

        GLib.timeout_add(int(args.dump_after * 1000), dump)
    Gtk.main()


if __name__ == "__main__":
    main()
