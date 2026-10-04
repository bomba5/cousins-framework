"""The suite is sealed off from the shell that runs it.

An operator's shell exports FRAMEWORK_ROOT (install.md says so) and a
cousin's shell exports COUSIN_HOME. Tests that followed them read the
live install's config/embedding.toml and failed on a finished install.
These canaries run the suite's own machinery in a subprocess whose
environment points at a root that would change the answers.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

import tests  # noqa: F401  installs the hermetic wrapper for the suite
from tests import _hermetic
from tests._fakes import fake_embedder

REPO = pathlib.Path(__file__).resolve().parent.parent


def _run_unittest(args, env_extra, cwd=REPO):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "unittest"] + args,
        cwd=str(cwd), env=env, capture_output=True, text=True,
        timeout=300)


class TestHermeticWrapper(unittest.TestCase):
    def test_wrapper_is_installed_for_the_suite(self):
        self.assertTrue(_hermetic.installed())

    def test_framework_variables_are_absent_inside_a_test(self):
        for name in _hermetic.HERMETIC_VARS:
            self.assertNotIn(name, os.environ)

    def test_inherited_and_leaked_variables_never_reach_a_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = pathlib.Path(tmp) / "test_probe_env.py"
            mod.write_text(textwrap.dedent("""\
                import os
                import unittest
                import tests  # noqa: F401

                class TestProbe(unittest.TestCase):
                    def test_a_leaks(self):
                        self.assertNotIn("FRAMEWORK_ROOT", os.environ)
                        self.assertNotIn("COUSIN_HOME", os.environ)
                        os.environ["FRAMEWORK_ROOT"] = "/leaked"

                    def test_b_sees_no_leak(self):
                        self.assertNotIn("FRAMEWORK_ROOT", os.environ)
                """))
            proc = _run_unittest(
                ["discover", "-s", tmp, "-t", tmp],
                {"FRAMEWORK_ROOT": tmp, "COUSIN_HOME": tmp,
                 "PYTHONPATH": str(REPO)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Ran 2 tests", proc.stderr)


class TestLeakGuard(unittest.TestCase):
    """The wrapper collects each test's garbage as the test ends: a leaked
    object's ResourceWarning is reported against the test that leaked it,
    never inside a later test's capture. With tracemalloc on, one that
    cousin_lib allocated fails that test; one the test opened itself is
    only reported.

    The cousin_lib probe leaks a sqlite connection, which warns when
    unclosed only from Python 3.13; the test's own leak is a file, which
    warns on every version."""

    WARNS_ON_SQLITE = sys.version_info >= (3, 13)

    PROBE = textwrap.dedent("""\
        import os
        import pathlib
        import tempfile
        import unittest
        import tests  # noqa: F401
        from cousin_lib.server.storage import ChatStore

        class TestProbe(unittest.TestCase):
            def test_a_product_object_left_open(self):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                store = ChatStore(pathlib.Path(tmp.name) / "chat.db")
                store.again = store          # a cycle: only the collector frees it

            def test_b_the_tests_own_file_left_open(self):
                held = [open(os.devnull)]
                held.append(held)

            def test_c_clean(self):
                pass
        """)

    def _probe(self, env):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "test_probe_leak.py").write_text(self.PROBE)
            return _run_unittest(["discover", "-s", tmp, "-t", tmp],
                                 dict(env, PYTHONPATH=str(REPO)))

    @unittest.skipUnless(WARNS_ON_SQLITE, "an unclosed sqlite connection warns from 3.13")
    def test_with_tracemalloc_a_cousin_lib_leak_fails_its_own_test(self):
        proc = self._probe({"PYTHONTRACEMALLOC": "20"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("FAILED (failures=1)", proc.stderr)
        self.assertIn("FAIL: test_a_product_object_left_open", proc.stderr)
        self.assertIn("allocated at cousin_lib/server/storage.py:", proc.stderr)
        self.assertIn("left open by test_probe_leak.TestProbe.test_b", proc.stderr)

    def test_without_tracemalloc_a_leak_is_only_reported(self):
        proc = self._probe({"PYTHONTRACEMALLOC": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Ran 3 tests", proc.stderr)
        if self.WARNS_ON_SQLITE:
            self.assertIn("left open by test_probe_leak.TestProbe.test_a", proc.stderr)
        self.assertIn("left open by test_probe_leak.TestProbe.test_b", proc.stderr)


class TestRestoreNeverEmptiesTheEnvironment(unittest.TestCase):
    """Threads a test leaves running (a node's poll loop, a server)
    read os.environ while the wrapper restores it. A clear-and-refill
    restore showed them an empty environment for a moment, which made
    hive node tests flaky; the restore touches only changed keys."""

    def test_a_concurrent_reader_always_sees_path(self):
        import threading
        stop = threading.Event()
        missing = []

        def reader():
            while not stop.is_set():
                if "PATH" not in os.environ:
                    missing.append(1)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for i in range(300):
                with _hermetic.hermetic_env():
                    os.environ["COUSIN_HERMETIC_PROBE"] = str(i)
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertEqual(missing, [])
        self.assertNotIn("COUSIN_HERMETIC_PROBE", os.environ)


class TestMemorySearchIgnoresAnExportedRoot(unittest.TestCase):
    """The regression from the install re-test: an exported root whose
    config/embedding.toml answers gave the keyword-only tests semantic
    hits."""

    def _root_with_embedding(self, tmp, url):
        root = pathlib.Path(tmp) / "live"
        (root / "config").mkdir(parents=True)
        (root / "cousins" / "testa").mkdir(parents=True)
        (root / "config" / "embedding.toml").write_text(
            'url = "%s"\nmodel = "m"\ntimeout_s = 2\n' % url)
        return root

    def test_reachable_embedder_under_exported_root(self):
        with tempfile.TemporaryDirectory() as tmp, fake_embedder() as url:
            root = self._root_with_embedding(tmp, url)
            proc = _run_unittest(
                ["discover", "-s", "tests", "-t", ".",
                 "-p", "test_memory_search.py"],
                {"FRAMEWORK_ROOT": str(root),
                 "COUSIN_HOME": str(root / "cousins" / "testa")})
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])

    def test_unreachable_embedder_under_exported_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_embedding(
                tmp, "http://127.0.0.1:9/api/embeddings")
            proc = _run_unittest(
                ["discover", "-s", "tests", "-t", ".",
                 "-p", "test_memory_search.py"],
                {"FRAMEWORK_ROOT": str(root)})
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
