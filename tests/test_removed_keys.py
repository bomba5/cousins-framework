"""The configuration 2.0.0 removed with the legacy tmux lane: one
table, cousin_lib/removed_keys, named where an operator looks (the
runner's start, `cousin-supervisor status`, the console's row,
`cousin-migrate plan|check`) and never fatal; `cousin-migrate tidy`
removes the lines, keeping everything else byte for byte and the prior
bytes beside the file, and stops a 1.x chat server still running for the
cousin. Invented cast only; no live process is ever signalled here but
the test's own."""
import contextlib
import io
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock

from cousin_lib import migrate, removed_keys
from cousin_lib.delivery import lane_refusal
from tests._hermetic import HermeticCase

RUNNER = '\n[agent]\nrunner = "fake"\n'

# A cousin.toml as 1.x spawned and migrated it: the [chat] table, the
# [runtime] table the migration carried and never removed, comments,
# the kept tables around them.
COUSIN_1X = (
    '[cousin]\n'
    'slug = "wren"\n'
    'name = "Wren"\n'
    'role = "archivist"\n'
    '\n'
    '# the operator\'s own comment stays\n'
    '[chat]\n'
    'port = 8091\n'
    'tmux_session = "wren"\n'
    '\n'
    '[operator]\n'
    'name = "Priya"\n'
    '\n'
    '[runtime]\n'
    'model = "opus"\n'
    'effort = "high"   # the tmux lane read this\n'
    'session_id = "s-old"\n'
    '\n'
    '[agent]\n'
    'runner = "fake"\n'
    'model = "opus"\n'
)

# The same file with the removed lines gone, and nothing else changed.
COUSIN_TIDY = (
    '[cousin]\n'
    'slug = "wren"\n'
    'name = "Wren"\n'
    'role = "archivist"\n'
    '\n'
    '# the operator\'s own comment stays\n'
    '[operator]\n'
    'name = "Priya"\n'
    '\n'
    '[agent]\n'
    'runner = "fake"\n'
    'model = "opus"\n'
)

HARNESS_1X = (
    '# where the harness keeps its files\n'
    'transcripts_dir = "~/t/{home_encoded}"\n'
    'flip_when_transcript_mb = 100\n'
    'settings_file = "~/s.json"\n'
    '\n'
    'attention_patterns = [\n'
    '    "Select login method",\n'
    '    "Paste code here",\n'
    ']\n'
    'busy_patterns = ["esc to interrupt"]\n'
    '\n'
    '[input_mode]\n'
    'normal_marker = "-- NORMAL --"\n'
    'insert_keys = "i"\n'
    '\n'
    '[agent]\n'
    'default_model = "opus"\n'
    '\n'
    '# how agent-cmd resumes\n'
    '[agent.resume]\n'
    'session_arg = "--session-id {session_id}"\n'
    'resume_arg = "--resume {session_id}"\n'
    '\n'
    '[auth.api_key]\n'
    'key_env = "HARNESS_KEY"\n'
    'exclude = [\n'
    '    ".credentials.json",\n'
    ']\n'
)

HARNESS_TIDY = (
    '# where the harness keeps its files\n'
    'transcripts_dir = "~/t/{home_encoded}"\n'
    'settings_file = "~/s.json"\n'
    '\n'
    '\n'
    '[agent]\n'
    'default_model = "opus"\n'
    '\n'
    '# how agent-cmd resumes\n'
)


def _root(case):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    (root / "cousins").mkdir()
    (root / "config").mkdir()
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)})
    p.start()
    case.addCleanup(p.stop)
    return root


def _home(root, slug, toml):
    home = root / "cousins" / slug
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "cousin.toml").write_text(toml)
    return home


class TestScan(HermeticCase):
    COUSIN = (
        ('[chat]\nport = 8091\n', "[chat] port"),
        ('[chat]\nhost = "127.0.0.1"\n', "[chat] host"),
        ('[chat]\ntmux_session = "wren"\n', "[chat] tmux_session"),
        ('[runtime]\nmodel = "opus"\n', "[runtime] model"),
        ('[runtime]\neffort = "high"\n', "[runtime] effort"),
        ('[runtime]\nsession_id = "s-1"\n', "[runtime] session_id"),
        ('[runtime]\nauth = "claude"\n', "[runtime] auth"),
    )
    HARNESS = (
        ('attention_patterns = ["Select login method"]\n', "attention_patterns"),
        ('busy_patterns = ["esc"]\n', "busy_patterns"),
        ('[input_mode]\nnormal_marker = "-- NORMAL --"\ninsert_keys = "i"\n', "[input_mode]"),
        ('flip_when_transcript_mb = 100\n', "flip_when_transcript_mb"),
        ('[agent.resume]\nsession_arg = "--session-id {session_id}"\n', "[agent.resume]"),
        ('[auth.api_key]\nkey_env = "HARNESS_KEY"\n', "[auth.api_key]"),
    )

    def test_scan_names_each_removed_key(self):
        for text, key in self.COUSIN:
            with self.subTest(key=key):
                root = _root(self)
                home = _home(root, "wren", '[cousin]\nslug = "wren"\n' + RUNNER + "\n" + text)
                found = removed_keys.scan(root, home)
                self.assertEqual([(f["where"], f["key"]) for f in found],
                                 [("cousin.toml", key)])
                self.assertEqual(sorted(found[0]), ["key", "line", "where"])
                self.assertTrue(found[0]["line"].strip())
                self.assertEqual(removed_keys.scan_home(home), found)
        for text, key in self.HARNESS:
            with self.subTest(key=key):
                root = _root(self)
                (root / "config" / "harness.toml").write_text(text)
                found = removed_keys.scan(root)
                self.assertEqual([(f["where"], f["key"]) for f in found],
                                 [("config/harness.toml", key)])
                self.assertTrue(found[0]["line"].strip())
        root = _root(self)
        (root / "config" / "hive.toml").write_text(
            'enabled = false\nhome_chat_url = "http://127.0.0.1:1"\n')
        self.assertEqual([(f["where"], f["key"]) for f in removed_keys.scan(root)],
                         [("config/hive.toml", "home_chat_url")])
        root = _root(self)
        (root / "config" / "agent-cmd").write_text("claude --model {model}\n")
        self.assertEqual([(f["where"], f["key"]) for f in removed_keys.scan(root)],
                         [("config/agent-cmd", "config/agent-cmd")])

    def test_scan_ignores_every_kept_key(self):
        root = _root(self)
        (root / "config" / "harness.toml").write_text(
            'transcripts_dir = "~/t"\nauto_memory_dir = "~/a"\nmcp_logs_dir = "~/m"\n'
            'settings_file = "~/s.json"\nhost_label = "box"\ndefault_flip_at = "04:00"\n'
            '\n[agent]\ndefault_model = "opus"\ndefault_effort = "high"\n'
            'models = ["opus"]\ncommit_attribution = false\n')
        (root / "config" / "hive.toml").write_text(
            'enabled = true\npublic_url = "http://127.0.0.1:8600"\nhome_cousin = "wren"\n'
            'checkin_seconds = 60\n')
        (root / "config" / "worker-cmd").write_text("true\n")
        home = _home(root, "wren", (
            '[cousin]\nslug = "wren"\nname = "Wren"\nrole = "r"\nhidden = false\n'
            '\n[operator]\nname = "Priya"\n\n[heartbeat]\ncontext_beat_seconds = 60\n'
            '\n[memory]\nscope = "shared"\n\n[lifecycle]\nflip_at = "05:00"\n'
            '\n[telegram]\nenabled = false\n'
            '\n[agent]\nrunner = "tmux"\naccount = "team"\nmodel = "opus"\neffort = "high"\n'
            'env_allow = ["LANG"]\nauto_start = false\ncommit_attribution = true\n'
            '\n[agent.sessions]\noperator = "primary"\n'))
        self.assertEqual(removed_keys.scan(root, home), [])

    def test_an_unreadable_file_shows_nothing_here(self):
        root = _root(self)
        home = _home(root, "wren", '[chat\nport = 8091\n')
        (root / "config" / "harness.toml").write_text('attention_patterns = [\n')
        (root / "config" / "hive.toml").write_text('home_chat_url = \n')
        self.assertEqual(removed_keys.scan(root, home), [])

    def test_without_a_home_only_the_install_is_read(self):
        root = _root(self)
        _home(root, "wren", '[chat]\nport = 8091\n' + RUNNER)
        (root / "config" / "agent-cmd").write_text("claude\n")
        self.assertEqual([f["key"] for f in removed_keys.scan(root)], ["config/agent-cmd"])


class TestMigrateNamesThem(HermeticCase):
    """`cousin-migrate plan --to` and `check` carry the same findings, as
    `warn 2.0.0` lines, and neither is less ready or less ok for them."""

    def test_plan_and_check_name_them(self):
        root = _root(self)
        home = _home(root, "wren", '[cousin]\nslug = "wren"\n\n[chat]\nport = 8091\n'
                                   '\n[agent]\nrunner = "fake"\n')
        (root / "config" / "agent-cmd").write_text("claude\n")
        want = [("cousin.toml", "[chat] port"), ("config/agent-cmd", "config/agent-cmd")]
        c = migrate.check(home, root=root, cli_version=lambda: "x")
        self.assertEqual([(f["where"], f["key"]) for f in c["removed"]], want)
        self.assertEqual(c["warnings"], [])
        p = migrate.switch_plan(home, root=root, to="sdk", supervisor_up=lambda root: True)
        self.assertEqual([(f["where"], f["key"]) for f in p["removed"]], want)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            migrate._print_removed(p["removed"], "  ")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("  warn 2.0.0 cousin.toml [chat] port: "))
        self.assertTrue(lines[1].startswith("  warn 2.0.0 config/agent-cmd: "))


class _Seams:
    """tidy's live actions: no process is ever signalled unless a test says
    which pid is a chat server."""

    def __init__(self, servers=(), alive=True, port_pid=None):
        self.servers, self.alive, self.port = set(servers), alive, port_pid
        self.killed = []

    def kw(self):
        return dict(pid_alive=lambda pid: self.alive and pid not in self.killed,
                    is_chat_server=lambda pid, home: pid in self.servers,
                    port_pid=lambda port: self.port,
                    kill=lambda pid, sig: self.killed.append(pid))


class TidyCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.root = _root(self)
        self.seams = _Seams()
        p = mock.patch.object(migrate, "_tidy_live", lambda: self.seams.kw())
        p.start()
        self.addCleanup(p.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = migrate.migrate_main(["tidy", *argv])
        return rc, out.getvalue(), err.getvalue()


class TestTidy(TidyCase):
    def test_tidy_writes_nothing_without_yes(self):
        home = _home(self.root, "wren", COUSIN_1X)
        (home / "data" / "chat-server.pid").write_text("4242\n")
        self.seams.servers = {4242}
        (self.root / "config" / "harness.toml").write_text(HARNESS_1X)
        (self.root / "config" / "agent-cmd").write_text("claude\n")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        rc, out, _ = self.cli("--all")
        self.assertEqual(rc, 0)
        for key in ("[chat] port", "[chat] tmux_session", "[runtime] model",
                    "[runtime] effort", "[runtime] session_id", "attention_patterns",
                    "busy_patterns", "[input_mode]", "flip_when_transcript_mb",
                    "[agent.resume]", "[auth.api_key]", "config/agent-cmd", "4242"):
            self.assertIn(key, out)
        self.assertIn("--yes", out)
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(self.seams.killed, [])

    def test_tidy_removes_exactly_the_removed_lines_and_keeps_the_rest_byte_for_byte(self):
        home = _home(self.root, "wren", COUSIN_1X)
        os.chmod(home / "cousin.toml", 0o640)
        (self.root / "config" / "harness.toml").write_text(HARNESS_1X)
        rc, _, err = self.cli("--all", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual((home / "cousin.toml").read_text(), COUSIN_TIDY)
        self.assertEqual((home / "cousin.toml").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.root / "config" / "harness.toml").read_text(), HARNESS_TIDY)
        self.assertEqual(removed_keys.scan(self.root, home), [])

    def test_tidy_keeps_crlf_and_a_table_with_other_keys(self):
        text = ('[cousin]\r\nslug = "wren"\r\n\r\n[chat]\r\nport = 8091\r\nnote = "mine"\r\n'
                + RUNNER.replace("\n", "\r\n"))
        home = _home(self.root, "wren", text)
        rc, _, err = self.cli("wren", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual((home / "cousin.toml").read_bytes(),
                         text.replace("port = 8091\r\n", "").encode())

    def test_tidy_keeps_the_prior_bytes(self):
        home = _home(self.root, "wren", COUSIN_1X)
        (self.root / "config" / "harness.toml").write_text(HARNESS_1X)
        (self.root / "config" / "hive.toml").write_text(
            'enabled = false\nhome_chat_url = "http://127.0.0.1:1"\n')
        (self.root / "config" / "agent-cmd").write_text("claude --model {model}\n")
        rc, _, err = self.cli("--all", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual((home / "data" / "cousin.toml.pre-2.0.0").read_text(), COUSIN_1X)
        self.assertEqual((self.root / "config" / "harness.toml.pre-2.0.0").read_text(),
                         HARNESS_1X)
        self.assertEqual((self.root / "config" / "hive.toml.pre-2.0.0").read_text(),
                         'enabled = false\nhome_chat_url = "http://127.0.0.1:1"\n')
        self.assertEqual((self.root / "config" / "hive.toml").read_text(), 'enabled = false\n')
        self.assertFalse((self.root / "config" / "agent-cmd").exists())
        self.assertEqual((self.root / "config" / "agent-cmd.pre-2.0.0").read_text(),
                         "claude --model {model}\n")
        # a later tidy never overwrites the first prior copy
        (home / "cousin.toml").write_text(COUSIN_TIDY + '\n[chat]\nport = 8092\n')
        rc, _, _ = self.cli("wren", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual((home / "data" / "cousin.toml.pre-2.0.0").read_text(), COUSIN_1X)
        self.assertIn("port = 8092", (home / "data" / "cousin.toml.pre-2.0.0.1").read_text())

    def test_tidy_all_covers_every_cousin_and_the_install(self):
        wren = _home(self.root, "wren", COUSIN_1X)
        sam = _home(self.root, "sam", '[cousin]\nslug = "sam"\n\n[chat]\nport = 8092\n' + RUNNER)
        toki = _home(self.root, "toki",
                     '[cousin]\nslug = "toki"\ntype = "worker"\n\n[chat]\nport = 8093\n')
        (self.root / "config" / "harness.toml").write_text(HARNESS_1X)
        (self.root / "config" / "agent-cmd").write_text("claude\n")
        rc, out, _ = self.cli("--all")
        for name in ("wren", "sam", "toki", "install"):
            self.assertIn(name, out)
        rc, _, err = self.cli("--all", "--yes")
        self.assertEqual(rc, 0, err)
        for home in (wren, sam, toki):
            self.assertEqual(removed_keys.scan_home(home), [], home.name)
        self.assertEqual(removed_keys.scan(self.root), [])
        rc, out, _ = self.cli("--all")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "nothing to tidy")
        # one slug touches that cousin only, never the install
        (self.root / "config" / "agent-cmd").write_text("claude\n")
        (sam / "cousin.toml").write_text('[cousin]\nslug = "sam"\n\n[chat]\nport = 8092\n' + RUNNER)
        rc, _, _ = self.cli("sam", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(removed_keys.scan_home(sam), [])
        self.assertTrue((self.root / "config" / "agent-cmd").exists())

    def test_tidy_refuses_a_cousin_with_no_runner(self):
        text = '[cousin]\nslug = "priya"\n\n[chat]\nport = 8094\ntmux_session = "priya"\n'
        priya = _home(self.root, "priya", text)
        rc, out, err = self.cli("priya", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn(lane_refusal(priya), err)
        self.assertEqual((priya / "cousin.toml").read_text(), text)
        self.assertFalse((priya / "data" / "cousin.toml.pre-2.0.0").exists())
        # --all names it, tidies the others, and says not everything was
        wren = _home(self.root, "wren", COUSIN_1X)
        rc, out, _ = self.cli("--all", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn(lane_refusal(priya), out)
        self.assertEqual((priya / "cousin.toml").read_text(), text)
        self.assertEqual((wren / "cousin.toml").read_text(), COUSIN_TIDY)

    def test_a_slug_or_all_exactly_one(self):
        for argv in ((), ("wren", "--all")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.cli(*argv)
        rc, _, err = self.cli("mallory")
        self.assertEqual(rc, 2)
        self.assertIn("mallory", err)

    def test_a_file_the_line_remover_cannot_edit_is_left_whole_and_named(self):
        # an inline table holding a removed key: no line to take out
        text = 'chat = {port = 8091}\n\n[cousin]\nslug = "wren"\n' + RUNNER
        home = _home(self.root, "wren", text)
        rc, out, err = self.cli("wren", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("by hand", out + err)
        self.assertEqual((home / "cousin.toml").read_text(), text)


class TestTidyChatServer(TidyCase):
    def test_tidy_stops_a_live_chat_server_and_only_that(self):
        home = _home(self.root, "wren", COUSIN_1X)
        pid_file = home / "data" / "chat-server.pid"
        # a pid file whose process is not a chat server: never signalled
        pid_file.write_text("5151\n")
        self.seams.servers = set()
        rc, out, _ = self.cli("wren", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(self.seams.killed, [])
        self.assertFalse(pid_file.exists())                  # stale: removed
        # the port answers for a process that is not a chat server either
        (home / "cousin.toml").write_text(COUSIN_1X)
        self.seams.port = 6161
        rc, _, _ = self.cli("wren", "--yes")
        self.assertEqual(self.seams.killed, [])
        # the pid file names a chat server: it is stopped (SIGTERM), once
        (home / "cousin.toml").write_text(COUSIN_1X)
        pid_file.write_text("4242\n")
        self.seams.servers, self.seams.port = {4242}, None
        rc, out, _ = self.cli("wren")
        self.assertEqual(self.seams.killed, [])              # plan: nothing signalled
        self.assertIn("4242", out)
        rc, out, _ = self.cli("wren", "--yes")
        self.assertEqual((rc, self.seams.killed), (0, [4242]))
        self.assertFalse(pid_file.exists())
        # no pid file (a unit started it): found by the port, before the key goes
        (home / "cousin.toml").write_text(COUSIN_1X)
        self.seams.servers, self.seams.port, self.seams.killed = {7373}, 7373, []
        rc, _, _ = self.cli("wren", "--yes")
        self.assertEqual(self.seams.killed, [7373])

    def test_the_live_check_signals_only_this_homes_chat_server(self):
        if not os.path.isdir("/proc"):
            self.skipTest("the chat-server check reads /proc")
        home = _home(self.root, "wren", COUSIN_1X)
        other = _home(self.root, "sam", COUSIN_1X)

        def proc(*argv):
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])
            self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
            return p

        ours = proc("cousin_lib.server.app", "--home", str(home))
        theirs = proc("cousin_lib.server.app", "--home", str(other))
        plain = proc("--home", str(home))
        time.sleep(0.2)
        check = migrate._chat_server_of
        self.assertTrue(check(ours.pid, home))
        self.assertFalse(check(theirs.pid, home))            # another home's server
        self.assertFalse(check(plain.pid, home))             # not a chat server
        self.assertFalse(check(2 ** 22 + 7, home))           # no such process


if __name__ == "__main__":
    unittest.main()
