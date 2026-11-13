"""Pure-Python core of the gnome-uebersicht daemon (no GTK/WebKit imports),
so it can be unit-tested anywhere."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {"node_modules", "lib", "src", ".git"}
ESBUILD_FLAGS_FILE = HERE / "esbuild-flags.json"


def _xdg(var: str, fallback: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v else Path.home() / fallback


def default_widgets_dir() -> Path:
    env = os.environ.get("UBERSICHT_WIDGETS_DIR")
    if env:
        return Path(env).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / "uebersicht" / "widgets"


def state_file() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "gnome-uebersicht" / "state.json"


def load_state(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or state_file()).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict, path: Path | None = None) -> None:
    f = path or state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(f)


def find_esbuild(explicit: str | None = None) -> str | None:
    cands = [explicit, os.environ.get("UBERSICHT_ESBUILD")]
    # the extension is usually a symlink into the repo — node_modules lives in the repo root
    for base in (HERE, HERE.parent):
        cands.append(str(base / "node_modules" / ".bin" / "esbuild"))
    cands.append(shutil.which("esbuild"))
    for c in cands:
        if c and os.access(c, os.X_OK):
            return c
    return None


def esbuild_argv(esbuild: str, widget: Path) -> list[str]:
    flags = json.loads(ESBUILD_FLAGS_FILE.read_text())
    return [esbuild, str(widget), *flags]


class Widgets:
    """Widget discovery in a directory (following Übersicht conventions):
    `*.jsx` files anywhere, plus `index.js(x)` inside `*.widget` folders;
    dot/underscore entries and node_modules/lib/src folders are skipped."""

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
        """Resolve a widget id or asset path; None if missing or outside the root."""
        p = (self.root / wid).resolve()
        try:
            p.relative_to(self.root.resolve())
        except ValueError:
            return None
        return p if p.is_file() else None

    def visible(self, hidden: set[str]) -> list[str]:
        return [i for i in self.ids() if i not in hidden]
