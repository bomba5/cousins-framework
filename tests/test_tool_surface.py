"""The tool-surface manifest.

The script list is derived, never typed: from the installed package's
entry points when it is installed, else from pyproject.toml in the
checkout. A CLI cannot ship without appearing in the manifest, and the
manifest cannot name a CLI that does not exist.
"""
import contextlib
import io
import os
import pathlib
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib.tool_surface import (
    MANIFEST_RELPATH,
    build_manifest,
    console_scripts,
    describe,
    scripts_from_pyproject,
    tool_surface_main,
    write_manifest,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestScriptDiscovery(unittest.TestCase):
    def test_pyproject_fallback_reads_project_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "pyproject.toml"
            path.write_text(
                '[project]\nname = "x"\n[project.scripts]\n'
                'cousin-alpha = "cousin_lib.alpha:main"\n'
                'cousin-beta = "cousin_lib.beta:beta_main"\n')
            self.assertEqual(
                scripts_from_pyproject(path),
                [("cousin-alpha", "cousin_lib.alpha:main"),
                 ("cousin-beta", "cousin_lib.beta:beta_main")])

    def test_discovery_matches_the_repository_pyproject(self):
        # Whether it came from entry points or the file, the set must
        # equal what pyproject declares: nothing hardcoded anywhere.
        declared = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text()
        )["project"]["scripts"]
        found = dict(console_scripts())
        self.assertEqual(found, declared)

    def test_missing_pyproject_is_an_empty_list(self):
        self.assertEqual(
            scripts_from_pyproject(pathlib.Path("/nonexistent/p.toml")),
            [])


class TestDescribe(unittest.TestCase):
    def test_first_help_line_of_a_real_script(self):
        line = describe("cousin-loops", "cousin_lib.loops:loops_main")
        self.assertTrue(line.startswith("usage: cousin-loops"), line)
        self.assertNotIn("\n", line)

    def test_wrapped_usage_is_joined_into_one_line(self):
        line = describe("cousin-memory", "cousin_lib.memory:memory_main")
        self.assertIn("{search,", line)
        self.assertNotIn("\n", line)

    def test_a_script_without_help_degrades_to_a_marker(self):
        line = describe("cousin-nope", "cousin_lib.does_not_exist:main")
        self.assertEqual(line, "(no --help available)")

    def test_bin_dir_runs_the_installed_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = pathlib.Path(tmp) / "cousin-fake"
            wrapper.write_text("#!/bin/sh\necho 'usage: cousin-fake [-h]'\n")
            wrapper.chmod(0o755)
            self.assertEqual(
                describe("cousin-fake", "x:y", bin_dir=tmp),
                "usage: cousin-fake [-h]")


class TestManifest(unittest.TestCase):
    def test_one_line_per_script_in_order(self):
        text = build_manifest(
            [("cousin-b", "b:m"), ("cousin-a", "a:m")],
            describe=lambda name, target, bin_dir=None: "usage: " + name)
        lines = [l for l in text.splitlines() if l.startswith("- `")]
        self.assertEqual(lines, [
            "- `cousin-b` - usage: cousin-b",
            "- `cousin-a` - usage: cousin-a",
        ])
        self.assertTrue(text.startswith("# Tool Surface"))
        self.assertIn("--help", text)

    def test_write_lands_under_root_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_manifest(tmp, "# Tool Surface\n")
            self.assertEqual(path, pathlib.Path(tmp) / MANIFEST_RELPATH)
            self.assertEqual(path.read_text(), "# Tool Surface\n")

    def test_write_is_atomic_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_manifest(tmp, "one\n")
            write_manifest(tmp, "two\n")
            data = pathlib.Path(tmp) / "data"
            self.assertEqual(sorted(p.name for p in data.iterdir()),
                             ["tool-surface.md"])


class TestCli(unittest.TestCase):
    def _main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return tool_surface_main(argv)

    def test_writes_the_manifest_for_every_declared_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("cousin_lib.tool_surface.describe",
                            lambda name, target, bin_dir=None: "u " + name):
                rc = self._main(["--root", tmp])
            self.assertEqual(rc, 0)
            text = (pathlib.Path(tmp) / MANIFEST_RELPATH).read_text()
        declared = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text()
        )["project"]["scripts"]
        for name in declared:
            self.assertIn("- `%s` - u %s" % (name, name), text)

    def test_root_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": tmp}), \
                    mock.patch("cousin_lib.tool_surface.describe",
                               lambda *a, **k: "u"):
                self.assertEqual(self._main([]), 0)
            self.assertTrue((pathlib.Path(tmp) / MANIFEST_RELPATH).exists())

    def test_missing_root_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": ""}):
            self.assertEqual(self._main([]), 2)

    def test_no_scripts_found_is_a_failure_not_an_empty_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("cousin_lib.tool_surface.console_scripts",
                            lambda: []):
                self.assertEqual(self._main(["--root", tmp]), 1)
            self.assertFalse((pathlib.Path(tmp) / MANIFEST_RELPATH).exists())


if __name__ == "__main__":
    unittest.main()
