"""Root discovery from a cousin's own shell.

A cousin's agent shell may carry COUSIN_HOME and nothing else; the
live failure was cousin-chat stopping with "FRAMEWORK_ROOT is not
set". A home lives at <root>/cousins/<slug>, so when that grandparent
holds a config/ it is the root: FrameworkConfig owns that rule and
every entry point gets it.
"""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cousin_lib.config import FrameworkConfig, MissingConfigError

REPO = pathlib.Path(__file__).resolve().parent.parent


def _make_root(base):
    root = pathlib.Path(base) / "install"
    (root / "config").mkdir(parents=True)
    for slug, port in (("wren", 18501), ("testa", 18502)):
        home = root / "cousins" / slug
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n[chat]\nport = %d\n'
            % (slug, slug.capitalize(), port))
    return root


class RootCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.root = _make_root(self.tmp)
        self.home = self.root / "cousins" / "wren"
        env = {k: v for k, v in os.environ.items()
               if k not in ("FRAMEWORK_ROOT", "COUSIN_HOME")}
        env["COUSIN_HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # a working directory that is not a checkout
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)


class TestFrameworkConfigFromHome(RootCase):
    def test_from_env_derives_the_root_from_cousin_home(self):
        self.assertEqual(FrameworkConfig.from_env().root, self.root)

    def test_resolve_derives_the_root_from_cousin_home(self):
        self.assertEqual(FrameworkConfig.resolve().root, self.root)

    def test_framework_root_wins_over_cousin_home(self):
        other = self.tmp / "other"
        other.mkdir()
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(other)}):
            self.assertEqual(FrameworkConfig.from_env().root, other)
            self.assertEqual(FrameworkConfig.resolve().root, other)

    def test_flag_wins_over_everything(self):
        flag = self.tmp / "flag"
        self.assertEqual(FrameworkConfig.resolve(str(flag)).root, flag)

    def test_home_wins_over_the_working_directory(self):
        checkout = self.tmp / "checkout"
        (checkout / "templates").mkdir(parents=True)
        (checkout / "config").mkdir()
        (checkout / "templates" / "cousin-CLAUDE.template.md").write_text("")
        os.chdir(checkout)
        self.assertEqual(
            FrameworkConfig.resolve(cwd_fallback=True).root, self.root)

    def test_a_home_without_config_beside_cousins_names_no_root(self):
        stray = self.tmp / "loose" / "cousins" / "wren"
        stray.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(stray)}):
            with self.assertRaises(MissingConfigError):
                FrameworkConfig.from_env()
            with self.assertRaises(MissingConfigError):
                FrameworkConfig.resolve()

    def test_a_home_not_under_cousins_names_no_root(self):
        odd = self.root / "elsewhere" / "wren"
        odd.mkdir(parents=True)
        self.assertIsNone(FrameworkConfig.root_from_home(odd))
        self.assertIsNone(FrameworkConfig.root_from_home(None))

    def test_memory_search_uses_the_shared_rule(self):
        from cousin_lib import memory_search
        self.assertEqual(memory_search._root(), self.root)
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(self.tmp)}):
            self.assertIsNone(memory_search._root())


class TestEntryPointsWithOnlyCousinHome(RootCase):
    def _run(self, main, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_cousin_chat_list(self):
        from cousin_lib.chat import chat_main
        rc, out, err = self._run(chat_main, ["list"])
        self.assertEqual(rc, 0, err)
        self.assertIn("testa", out)
        self.assertIn("wren", out)

    def test_cousin_chat_list_as_a_subprocess(self):
        # The installed entry runs in a fresh interpreter: prove the
        # rule there too, not only in-process.
        env = {k: v for k, v in os.environ.items()}
        env["PYTHONPATH"] = str(REPO)
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; from cousin_lib.chat import chat_main;"
             " sys.exit(chat_main(['list']))"],
            env=env, cwd=str(self.tmp), capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("testa", proc.stdout)

    def test_cousin_job_list(self):
        from cousin_lib.jobs import jobs_main
        rc, _out, err = self._run(jobs_main, ["list"])
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.root / "data" / "jobs.db").exists())

    def test_cousin_schedule_list(self):
        from cousin_lib.schedule import schedule_main
        rc, _out, err = self._run(schedule_main, ["list"])
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.root / "data" / "scheduled.db").exists())

    def test_cousin_tracker_list(self):
        from cousin_lib.tracker import tracker_main
        rc, _out, err = self._run(tracker_main, ["list"])
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.root / "data" / "tracker.db").exists())

    def test_cousin_memory_search_reads_the_install_config(self):
        from cousin_lib.memory import memory_main
        (self.root / "config" / "embedding.toml").write_text(
            'url = "http://127.0.0.1:9/api/embeddings"\nmodel = "m"\n'
            'timeout_s = 1\n')
        (self.home / "memory").mkdir()
        (self.home / "memory" / "kettle.md").write_text(
            "The kettle descales monthly.\n")
        rc, out, err = self._run(memory_main, ["search", "kettle"])
        self.assertEqual(rc, 0, err)
        self.assertIn("kettle.md", out)
        # the semantic leg was attempted: its failure is announced
        self.assertIn("embedding", (out + err).lower())


if __name__ == "__main__":
    unittest.main()
