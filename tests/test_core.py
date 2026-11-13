"""Unit tests for extension/ubs_core.py (no GTK/WebKit needed)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extension"))

import ubs_core  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "widgets"


class WidgetDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.w = ubs_core.Widgets(FIXTURES)

    def test_finds_jsx_and_widget_folders(self):
        self.assertEqual(self.w.ids(), ["clock.widget/index.jsx", "stateful.jsx"])

    def test_skips_hidden_underscore_and_vendor_dirs(self):
        ids = self.w.ids()
        self.assertNotIn(".hidden.jsx", ids)
        self.assertFalse(any(i.startswith(("node_modules/", "lib/")) for i in ids))
        self.assertNotIn("clock.widget/helper.js", ids)

    def test_missing_root_gives_no_widgets(self):
        self.assertEqual(ubs_core.Widgets(FIXTURES / "nope").ids(), [])

    def test_visible_filters_hidden(self):
        self.assertEqual(self.w.visible({"stateful.jsx"}), ["clock.widget/index.jsx"])

    def test_path_resolves_files_inside_root(self):
        self.assertEqual(self.w.path("stateful.jsx"), (FIXTURES / "stateful.jsx").resolve())
        self.assertEqual(self.w.path("_helper.js"), (FIXTURES / "_helper.js").resolve())

    def test_path_rejects_traversal_and_missing(self):
        self.assertIsNone(self.w.path("../test_core.py"))
        self.assertIsNone(self.w.path("/etc/passwd"))
        self.assertIsNone(self.w.path("missing.jsx"))
        self.assertIsNone(self.w.path("clock.widget"))  # directories are not served


class StateTest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sub" / "state.json"
            ubs_core.save_state({"hidden_widgets": ["a.jsx"], "desktop_hidden": True}, f)
            self.assertEqual(ubs_core.load_state(f), {"hidden_widgets": ["a.jsx"], "desktop_hidden": True})
            self.assertFalse((Path(d) / "sub" / "state.tmp").exists())

    def test_missing_or_corrupt_state_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "state.json"
            self.assertEqual(ubs_core.load_state(f), {})
            f.write_text("{not json")
            self.assertEqual(ubs_core.load_state(f), {})
            f.write_text("[1, 2]")
            self.assertEqual(ubs_core.load_state(f), {})

    def test_default_locations_follow_xdg(self):
        env = {"XDG_STATE_HOME": "/s", "XDG_DATA_HOME": "/d"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("UBERSICHT_WIDGETS_DIR", None)
            self.assertEqual(ubs_core.state_file(), Path("/s/gnome-uebersicht/state.json"))
            self.assertEqual(ubs_core.default_widgets_dir(), Path("/d/uebersicht/widgets"))
        with mock.patch.dict(os.environ, {"UBERSICHT_WIDGETS_DIR": "/w"}):
            self.assertEqual(ubs_core.default_widgets_dir(), Path("/w"))


class EsbuildTest(unittest.TestCase):
    def test_argv_uses_shared_flags(self):
        argv = ubs_core.esbuild_argv("/bin/esbuild", Path("/w/x.jsx"))
        self.assertEqual(argv[:2], ["/bin/esbuild", "/w/x.jsx"])
        self.assertEqual(argv[2:], json.loads(ubs_core.ESBUILD_FLAGS_FILE.read_text()))
        self.assertIn("--external:uebersicht", argv)
        self.assertIn("--format=cjs", argv)

    def test_explicit_esbuild_wins(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "esbuild"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            self.assertEqual(ubs_core.find_esbuild(str(exe)), str(exe))


class ScaleRectsTest(unittest.TestCase):
    def test_identity_without_zoom(self):
        self.assertEqual(ubs_core.scale_rects([(10, 20, 30, 40)], (100, 100), (100, 100)), [(10, 20, 30, 40)])

    def test_text_scaling_zoom(self):
        # GNOME text-scaling-factor 1.1: viewport 2792 CSS px inside a 3072 px window
        [(x, y, w, h)] = ubs_core.scale_rects([(731.2, 100.2, 13.6, 15.5)], (2792, 1477), (3072, 1728))
        self.assertLessEqual(x, 731.2 * 3072 / 2792)
        self.assertGreaterEqual(x + w, (731.2 + 13.6) * 3072 / 2792)
        self.assertGreaterEqual(y + h, (100.2 + 15.5) * 1728 / 1477)

    def test_missing_viewport_falls_back_to_identity(self):
        self.assertEqual(ubs_core.scale_rects([(1, 2, 3, 4)], None, (50, 50)), [(1, 2, 3, 4)])
        self.assertEqual(ubs_core.scale_rects([(1, 2, 3, 4)], (0, 0), (50, 50)), [(1, 2, 3, 4)])


if __name__ == "__main__":
    unittest.main()
