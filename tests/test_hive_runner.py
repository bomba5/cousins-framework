"""The hive and a runner cousin (phase 6, task 9).

A hive node is not a framework install: it runs the standalone
templates/hive-node/cousin_node.py. The two places a cousin's lane can
matter on the hive are the ones pinned here:

- a node's `[tell-home: ...]` posts to a local cousin's chat server
  (`/api/send`); for a runner cousin that server hands the message to
  the runner's inbox, and it must start without a tmux binary, which
  a runner cousin never uses;
- `cousin-hive recall` is answered by the queen from its own store; the
  only thing a lane changes is the environment the call runs in, so the
  round trip is run from the environment a runner gives its tools.

Loopback queen, in-process node, fake agent command, the real chat
server on port 0. No model call, no tmux.
"""
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from cousin_lib import accounts
from cousin_lib.hive import HiveStore, build_queen, hive_send
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.main import export_environment
from cousin_lib.server.app import StartupError, build_server
from tests._hermetic import HermeticCase

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_NODE_PY = _REPO_ROOT / "templates" / "hive-node" / "cousin_node.py"

# The node's brain: whatever the prompt, answer with a tell-home marker.
_TELL_HOME_AGENT = """\
import sys
sys.stdin.read()
print("On it. [tell-home: the greenhouse vent is stuck open]")
"""


def _load_node_module():
    spec = importlib.util.spec_from_file_location("cousin_node", _NODE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(case):
    """A throwaway install root with one runner cousin (Wren, `fake`)
    and one tmux cousin (Sam), each with a chat port of 0."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    homes = {}
    for slug, agent in (("wren", '[agent]\nrunner = "fake"\n'),
                        ("sam", "")):
        home = root / "cousins" / slug
        for sub in ("data", "run", "memory"):
            (home / sub).mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n\n[chat]\nport = 0\n\n%s'
            % (slug, slug.capitalize(), agent))
        homes[slug] = home
    return root, homes


def _inbox_rows(home):
    path = pathlib.Path(home) / "data" / "inbox.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        return [dict(zip(("thread_id", "source", "sender", "body", "state"), r))
                for r in conn.execute("SELECT thread_id, source, sender, body,"
                                      " state FROM inbox ORDER BY id")]
    except sqlite3.OperationalError:
        # the producer created the file and has not written its schema
        # yet: nothing to read on this poll
        return []
    finally:
        conn.close()


def _wait_rows(home, *, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _inbox_rows(home)
        if rows:
            return rows
        time.sleep(0.05)
    return []


def _post(port, path, body):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


class TestRunnerChatServerWithoutTmux(HermeticCase):
    def setUp(self):
        self.root, self.homes = _install(self)
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        # No tmux anywhere: PATH is an empty directory.
        empty = self.root / "empty-bin"
        empty.mkdir()
        os.environ["PATH"] = str(empty)

    def _started(self, server):
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_a_runner_cousins_chat_server_starts_without_tmux(self):
        # tmux resolved from PATH (absent) and named explicitly (missing);
        # each server's /api/send lands in the runner's inbox, not nowhere
        for n, tmux_bin in enumerate((None, str(self.root / "no-such" / "tmux"))):
            with self.subTest(tmux_bin=tmux_bin):
                server = self._started(
                    build_server(self.homes["wren"], tmux_bin=tmux_bin))
                status, _ = _post(server.port, "/api/send",
                                  {"user": "Priya", "message": "status %d" % n})
                self.assertEqual(status, 200)
        deadline = time.monotonic() + 8
        while len(_inbox_rows(self.homes["wren"])) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([(r["sender"], r["body"], r["state"])
                          for r in _inbox_rows(self.homes["wren"])],
                         [("Priya", "status 0", "queued"),
                          ("Priya", "status 1", "queued")])

    def test_a_tmux_cousins_chat_server_still_refuses_without_tmux(self):
        # guard: the tmux lane's startup check is unchanged
        for tmux_bin in (None, str(self.root / "no-such" / "tmux")):
            with self.subTest(tmux_bin=tmux_bin):
                with self.assertRaises(StartupError) as ctx:
                    build_server(self.homes["sam"], tmux_bin=tmux_bin)
                self.assertIn("tmux binary not found", str(ctx.exception))


class TestTellHomeToARunnerCousin(HermeticCase):
    def setUp(self):
        self.root, self.homes = _install(self)
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        self.store = HiveStore(self.root / "hive")
        self.addCleanup(self.store.close)
        self.queen = build_queen(self.store)
        self.queen.start()
        self.addCleanup(self.queen.stop)
        self.queen_url = "http://127.0.0.1:%d" % self.queen.port
        agent = self.root / "tell_home_agent.py"
        agent.write_text(_TELL_HOME_AGENT)
        self.agent_cmd = "%s %s" % (sys.executable, agent)
        self.node_dir = self.root / "testa-node"
        self.node_dir.mkdir()
        (self.node_dir / "CLAUDE.md").write_text("# Testa\nThe test node.\n")

    def test_a_nodes_tell_home_reaches_a_runner_cousins_inbox(self):
        # the runner cousin's REAL chat server, with no tmux binary
        server = build_server(self.homes["wren"],
                              tmux_bin=str(self.root / "no-such" / "tmux"))
        server.start()
        self.addCleanup(server.stop)
        node = _load_node_module().build_node({
            "COUSIN_SLUG": "testa", "NODE_NAME": "Testa", "NODE_PORT": "0",
            "NODE_HOST": "127.0.0.1", "NODE_DIR": str(self.node_dir),
            "QUEEN_URL": self.queen_url,
            "HIVE_TOKEN": self.store.mint_token("testa",
                                                scope=["own", "shared"]),
            "HOME_CHAT_URL": "http://127.0.0.1:%d" % server.port,
            "AGENT_CMD": self.agent_cmd, "NODE_POLL_SECONDS": "0.2",
        }, log=lambda text: None)
        node.start()
        self.addCleanup(node.stop)
        # a hive message to the node: its turn tells home
        hive_send(queen_url=self.queen_url,
                  token=self.store.mint_token("toki", scope=["own"]),
                  to="testa", body="how is the greenhouse", msg_id="t1")
        rows = _wait_rows(self.homes["wren"])
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["sender"], "Testa")
        self.assertEqual(rows[0]["body"], "the greenhouse vent is stuck open")
        self.assertEqual(rows[0]["source"], "chat")
        self.assertEqual(rows[0]["state"], "queued")
        # the runner's own reader sees the same row
        self.assertEqual(Inbox(self.homes["wren"]).pending(), 1)


class TestRecallFromARunnerToolEnvironment(HermeticCase):
    def test_recall_round_trip_from_a_runner_cousins_tool_environment(self):
        # guard: recall has no lane in it (the queen answers from its
        # store); this pins that the runner's tool environment (its
        # exported COUSIN_HOME/FRAMEWORK_ROOT, scrubbed credentials, the
        # home as the working directory) still reaches the queen
        root, homes = _install(self)
        home = homes["wren"]
        store = HiveStore(root / "hive")
        self.addCleanup(store.close)
        queen = build_queen(store)
        queen.start()
        self.addCleanup(queen.stop)
        store.append_memory("testa", "the greenhouse vent opens at 28C",
                            scope="shared")
        token = store.mint_token("wren", scope=["own", "shared"])
        os.environ["ANTHROPIC_API_KEY"] = "sk-not-a-real-key"
        export_environment(home)
        env = accounts.scrub(dict(os.environ))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["COUSIN_HOME"], str(home))
        self.assertEqual(env["FRAMEWORK_ROOT"], str(root))
        # the child imports this checkout, not whatever the venv installed
        env["PYTHONPATH"] = str(_REPO_ROOT)
        proc = subprocess.run(
            [sys.executable, "-m", "cousin_lib.hive", "recall",
             "--queen", "http://127.0.0.1:%d" % queen.port,
             "--token", token, "greenhouse"],
            cwd=str(home), env=env, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "- the greenhouse vent opens at 28C\n")


if __name__ == "__main__":
    unittest.main()
