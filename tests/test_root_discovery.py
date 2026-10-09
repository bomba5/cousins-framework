"""Root discovery from a cousin's own shell, and from a person's.

A cousin's agent shell may carry COUSIN_HOME and nothing else; the
live failure was cousin-chat stopping with "FRAMEWORK_ROOT is not
set". A home lives at <root>/cousins/<slug>, so when that grandparent
holds a config/ it is the root: FrameworkConfig owns that rule and
every entry point gets it.

A person's shell may carry neither: inside the checkout every command
finds the root from the working directory, outside any install each
exits 2 with one line naming what to set, and none prints a traceback
(the sweep at the bottom runs every installed command that way).
"""
import concurrent.futures
import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
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


# ---- no FRAMEWORK_ROOT, no COUSIN_HOME: the checkout, or one message ----

ROOT_MISSING = ("no framework root", "FRAMEWORK_ROOT is not set",
                "no registry")
SHARED_MESSAGE = "no framework root; pass --root <checkout>"


def _make_checkout(base):
    """The shape looks_like_checkout recognises, with the templates and
    config/ this version ships (what a fresh clone has)."""
    checkout = pathlib.Path(base) / "checkout"
    checkout.mkdir()
    shutil.copytree(REPO / "templates", checkout / "templates")
    shutil.copytree(REPO / "config", checkout / "config")
    return checkout


def _bare_env(user_home):
    env = {k: v for k, v in os.environ.items()
           if k != "FRAMEWORK_ROOT" and not k.startswith("COUSIN_")}
    env["HOME"] = str(user_home)
    return env


class BareCase(unittest.TestCase):
    """Neither FRAMEWORK_ROOT nor COUSIN_HOME set; a checkout and a
    directory outside any install to run from."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.checkout = _make_checkout(self.tmp)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.tmp / "user").mkdir()
        patcher = mock.patch.dict(os.environ, _bare_env(self.tmp / "user"),
                                  clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)

    def _run(self, main, argv, cwd):
        os.chdir(cwd)
        # a command exports the root it found; each run starts bare
        os.environ.pop("FRAMEWORK_ROOT", None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestForCommand(BareCase):
    def test_it_finds_the_checkout_and_exports_it(self):
        os.chdir(self.checkout)
        self.assertEqual(FrameworkConfig.for_command().root, self.checkout)
        self.assertEqual(os.environ["FRAMEWORK_ROOT"], str(self.checkout))
        # what library code reads from then on agrees
        self.assertEqual(FrameworkConfig.from_env().root, self.checkout)

    def test_outside_an_install_it_raises_the_shared_message(self):
        os.chdir(self.outside)
        with self.assertRaises(MissingConfigError) as ctx:
            FrameworkConfig.for_command()
        self.assertIn(SHARED_MESSAGE, str(ctx.exception))
        self.assertNotIn("FRAMEWORK_ROOT", os.environ)


class TestNamedCommandsFromTheCheckout(BareCase):
    """The three reported: two crashed (loops flips, shared templates)
    and one refused (watch) inside the checkout."""

    def test_loops_flips(self):
        from cousin_lib.loops import loops_main
        rc, out, err = self._run(loops_main, ["flips"], self.checkout)
        self.assertEqual(rc, 0, err)
        self.assertIn("install default:", out)
        rc, _out, err = self._run(loops_main, ["flips"], self.outside)
        self.assertEqual(rc, 2)
        self.assertIn("cousin-loops: " + SHARED_MESSAGE, err)

    def test_shared_templates(self):
        from cousin_lib.shared_tier import shared_main
        rc, out, err = self._run(shared_main, ["templates"], self.checkout)
        self.assertIn(rc, (0, 1), err)       # 1: a file differs, not a crash
        self.assertIn("config/law.md", out)
        rc, _out, err = self._run(shared_main, ["templates"], self.outside)
        self.assertEqual(rc, 2)
        self.assertIn("cousin-shared: " + SHARED_MESSAGE, err)

    def test_watch(self):
        from cousin_lib.watch import watch_main
        home = self.checkout / "cousins" / "wren"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[agent]\nrunner = "fake"\n')
        rc, _out, err = self._run(watch_main, ["wren"], self.checkout)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self._run(watch_main, ["wren"], self.outside)
        self.assertEqual(rc, 2)
        self.assertIn("cousin-watch: " + SHARED_MESSAGE, err)


# Every [project.scripts] entry, with a subcommand that reaches the
# command's root resolution (a real one, not --help), and what it says
# outside any install: the shared root message, the COUSIN_HOME a
# cousin-scoped command needs, or nothing (it needs no root). In argv,
# {tmp} is the test's directory and {cwd} the one the command runs from.
_HOME = "COUSIN_HOME"
COMMANDS = {
    "cousin-gate": ([], "--root"),                  # an explicit tree, by design
    "cousin-reply": (["--message", "hi"], _HOME),
    "cousin-chat": (["list"], SHARED_MESSAGE),
    "cousin-chat-import": (["--old-home", "{tmp}/old", "wren"],
                           SHARED_MESSAGE),
    "cousin-spawn": (["--sync-template", "wren"], SHARED_MESSAGE),
    "cousin-schedule": (["tick"], SHARED_MESSAGE),
    "cousin-job": (["list"], SHARED_MESSAGE),
    "cousin-tracker": (["list"], SHARED_MESSAGE),
    "cousin-meeting": (["list"], SHARED_MESSAGE),
    "cousin-memory": (["search", "kettle"], _HOME),
    "cousin-flip": (["--dry-run", "wren"], SHARED_MESSAGE),
    "cousin-account": (["list"], SHARED_MESSAGE),
    "cousin-version": ([], None),
    "cousin-reincarnate": (["--new-role", "a role", "wren"], SHARED_MESSAGE),
    "cousin-transplant": (["--donor", "wren", "--recipient", "testa",
                           "--mode", "merge"], SHARED_MESSAGE),
    "cousin-self-portrait": (["show"], _HOME),
    "cousin-shared": (["templates"], SHARED_MESSAGE),
    "cousin-loops": (["flips"], SHARED_MESSAGE),
    "cousin-health": ([], SHARED_MESSAGE),
    "cousin-upkeep": ([], SHARED_MESSAGE),
    "cousin-artifact": (["list"], SHARED_MESSAGE),
    "cousin-doctor": ([], SHARED_MESSAGE),
    "cousin-cycle": (["state"], _HOME),
    "cousin-session": (["status"], _HOME),
    "cousin-callback": (["list"], _HOME),
    "cousin-reason": (["list"], _HOME),
    "cousin-backup": (["--dest", "{tmp}/backups"], _HOME),
    "cousin-sync-state": ([], None),               # a deprecated no-op (3.47.0)
    "cousin-console": (["adduser"], SHARED_MESSAGE),
    "cousin-image": (["gen", "a cat"], SHARED_MESSAGE),
    "cousin-voice": (["gen", "hello"], SHARED_MESSAGE),
    "cousin-video": (["gen", "a cat"], SHARED_MESSAGE),
    "cousin-telegram": ([], _HOME),
    "cousin-hive": (["nodes"], SHARED_MESSAGE),
    "cousin-spawn-node": (["--queen-url", "http://127.0.0.1:9", "--name",
                           "Wren", "--role", "a role", "--token-file",
                           "{tmp}/token", "--out", "{cwd}/node.tgz",
                           "wren"], SHARED_MESSAGE),
    "cousin-cache-audit": ([], _HOME),
    "cousin-sweep": (["compact"], SHARED_MESSAGE),
    "cousin-tool-surface": (["--bin", "{tmp}/no-bin"], SHARED_MESSAGE),
    "cousin-mcp": (["--list-tools"], "no registry"),
    "cousin-runner": (["--home", "{cwd}/cousins/wren", "--check-auth"],
                      "cousin.toml"),
    "cousin-watch": (["wren"], SHARED_MESSAGE),
    "cousin-supervisor": (["status"], SHARED_MESSAGE),
    "cousin-migrate": (["check", "wren"], SHARED_MESSAGE),
    "cousin-upgrade": (["--dry-run", "--checkout", "{tmp}/no-checkout"],
                       SHARED_MESSAGE),
}


def _scripts():
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    return data["project"]["scripts"]


class TestEveryCommandWithoutARoot(BareCase):
    """Each installed command, run as its console script runs it (a
    fresh interpreter, the working directory not on sys.path) with
    neither variable set: from inside a checkout it finds the root,
    from outside any install it exits 2 with one line saying what to
    set. Never a traceback."""

    def test_the_table_names_every_installed_command(self):
        self.assertEqual(sorted(COMMANDS), sorted(_scripts()))

    def _launch(self, name, target, cwd):
        module, func = target.split(":")
        argv = [a.format(tmp=self.tmp, cwd=cwd) for a in COMMANDS[name][0]]
        code = ("import sys; from %s import %s as main;"
                " sys.exit(main(%r))" % (module, func, argv))
        env = dict(os.environ, PYTHONPATH=str(REPO))
        # -P: a console script does not put the working directory on
        # sys.path, so a checkout's own cousin_lib is never what runs
        proc = subprocess.run(
            [sys.executable, "-P", "-c", code], cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=120)
        return proc.returncode, proc.stdout, proc.stderr

    def _sweep(self, cwd):
        (self.tmp / "token").write_text("hive_token\n")
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            futures = {name: pool.submit(self._launch, name, target, cwd)
                       for name, target in _scripts().items()}
            return {name: f.result() for name, f in futures.items()}

    def test_from_inside_a_checkout_every_command_finds_the_root(self):
        for name, (rc, out, err) in sorted(self._sweep(self.checkout).items()):
            with self.subTest(command=name, argv=COMMANDS[name][0], rc=rc):
                self.assertNotIn("Traceback", err)
                for phrase in ROOT_MISSING:
                    self.assertNotIn(phrase, err + out)

    def test_outside_any_install_every_command_says_what_to_set(self):
        for name, (rc, out, err) in sorted(self._sweep(self.outside).items()):
            expected = COMMANDS[name][1]
            with self.subTest(command=name, argv=COMMANDS[name][0]):
                self.assertNotIn("Traceback", err)
                if expected is None:
                    self.assertEqual(rc, 0, err)
                    continue
                self.assertEqual(rc, 2, err)
                # the line it ends on says what to set
                last = (err.strip().splitlines() or [""])[-1]
                self.assertIn(expected, last, err)


if __name__ == "__main__":
    unittest.main()
