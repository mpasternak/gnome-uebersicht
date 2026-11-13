#!/usr/bin/env python3
"""gnome-uebersicht daemon: a transparent WebKitGTK window that renders
Übersicht-style widgets.

It is launched by the GNOME Shell extension (via Meta.WaylandClient), which then
pins the window to the desktop (DESKTOP type, all workspaces, below all windows).
It can also be started by hand for testing:  ./uebersicht_daemon.py --debug

Control from the extension: lines on stdin —
  reload | refresh | hide | show | inspect | quit | widget-hide <id> | widget-show <id>
Status for the extension: lines on stdout prefixed with "@@ubs " followed by JSON.
"""

import argparse
import ctypes
import json
import mimetypes
import os
import shutil
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("JavaScriptCore", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, JavaScriptCore, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNTIME_DIR = HERE / "runtime"
SCHEME = "ubersicht"
TITLE = "gnome-uebersicht"
SKIP_DIRS = {"node_modules", "lib", "src", ".git"}
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


def state_file() -> Path:
    return Path(GLib.get_user_state_dir()) / "gnome-uebersicht" / "state.json"


def load_state() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(f)


def default_widgets_dir() -> Path:
    env = os.environ.get("UBERSICHT_WIDGETS_DIR")
    if env:
        return Path(env).expanduser()
    return Path(GLib.get_user_data_dir()) / "uebersicht" / "widgets"


def find_esbuild(explicit: str | None) -> str | None:
    cands = [explicit, os.environ.get("UBERSICHT_ESBUILD")]
    # the extension is usually a symlink into the repo — node_modules lives in the repo root
    for base in (HERE, HERE.parent):
        cands.append(str(base / "node_modules" / ".bin" / "esbuild"))
    cands.append(shutil.which("esbuild"))
    for c in cands:
        if c and os.access(c, os.X_OK):
            return c
    return None


class Widgets:
    """Widget discovery in a directory (following Übersicht conventions)."""

    def __init__(self, root: Path):
        self.root = root

    def ids(self) -> list[str]:
        out = []
        if not self.root.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=True):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            rel = Path(dirpath).relative_to(self.root)
            in_widget_dir = any(p.endswith(".widget") for p in rel.parts)
            for f in sorted(filenames):
                if f.startswith((".", "_")):
                    continue
                if f.endswith(".jsx") or (in_widget_dir and f in ("index.js", "index.jsx")):
                    out.append(str(rel / f) if str(rel) != "." else f)
        return sorted(set(out))

    def path(self, wid: str) -> Path | None:
        p = (self.root / wid).resolve()
        try:
            p.relative_to(self.root.resolve())
        except ValueError:
            return None
        return p if p.is_file() else None


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

        self._build_window()
        self._watch_tree()
        self._watch_stdin()
        self._announce()

    # ---------------------------------------------------------------- window
    def _build_window(self):
        data_dir = Path(GLib.get_user_data_dir()) / "gnome-uebersicht"
        cache_dir = Path(GLib.get_user_cache_dir()) / "gnome-uebersicht"
        mgr = WebKit2.WebsiteDataManager(
            base_data_directory=str(data_dir), base_cache_directory=str(cache_dir)
        )
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
            f = (RUNTIME_DIR / path[len("/__ubs/"):]).resolve()
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

    def _apply_hidden(self):
        self.js(f"window.__ubs && window.__ubs.setHidden({json.dumps(self.hidden)})")

    def visible_ids(self) -> list[str]:
        return [i for i in self.widgets.ids() if i not in self.hidden_widgets]

    def _announce(self):
        """Tell the extension which widgets exist and which are hidden."""
        widgets = [{"id": i, "hidden": i in self.hidden_widgets} for i in self.widgets.ids()]
        print("@@ubs " + json.dumps({"widgets": widgets, "desktopHidden": self.hidden}), flush=True)

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
            self._set_region(msg.get("rects") or [])
            self._reply(reply, value, {"ok": True})
        else:
            self._reply(reply, value, {"error": f"unknown operation {op!r}"})
        return True

    def _reply(self, reply, value, obj):
        ctx = value.get_context()
        reply.return_value(JavaScriptCore.Value.new_from_json(ctx, json.dumps(obj)))

    def _spawn(self, argv, cwd, done, stdin_text=None):
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
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
        argv = [
            self.esbuild, str(p), "--bundle", "--format=cjs", "--platform=browser", "--target=es2020",
            "--jsx=transform", "--jsx-factory=__ubsReact.createElement", "--jsx-fragment=__ubsReact.Fragment",
            "--loader:.js=jsx", "--sourcemap=inline", "--log-level=error", "--charset=utf8",
            "--external:uebersicht", "--external:react", "--external:react-dom", "--external:react-dom/client",
            "--external:@emotion/css", "--external:@emotion/styled",
        ]

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
        if cmd in ("widget-hide", "widget-show") and arg:
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
            self.view.get_inspector().show()
        elif cmd == "quit":
            Gtk.main_quit()
        return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--widgets", default=str(default_widgets_dir()), help="widgets directory")
    ap.add_argument("--esbuild", default=None, help="path to the esbuild binary")
    ap.add_argument("--debug", action="store_true", help="regular decorated window, no click-through")
    ap.add_argument("--stdin-control", action="store_true", help="read control commands from stdin (used by the extension)")
    ap.add_argument("--dump-after", type=float, default=0, help="(testing) after N seconds print the page text and exit")
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
            d.view.evaluate_javascript(
                "JSON.stringify({text: document.body.innerText, html: document.getElementById('ubs-root').innerHTML.slice(0, 600),"
                " ls: localStorage.length})",
                -1, None, None, None, got)
            return False
        GLib.timeout_add(int(args.dump_after * 1000), dump)
    Gtk.main()


if __name__ == "__main__":
    main()
