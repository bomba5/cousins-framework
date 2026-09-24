"""A hive node's [tell-home: ...] through the queen (phase 10a, one inbound
surface): `POST /hive/tell-home` authenticates the node's bearer token,
takes the sender from the token (never the body), and delivers to the
install's home cousin only (config/hive.toml `home_cousin`), through
peer_inbound.accept (a replayed msg_id is refused). The node posts there
with its own token when its env says TELL_HOME=1."""
import json
import pathlib
import sqlite3
import time
import unittest
from unittest import mock

from cousin_lib import hive as hive_lib
from cousin_lib import spawn_node
from tests.console.test_hive_console import HiveConsoleCase


def _messages(home):
    path = pathlib.Path(home) / "data" / "chat.db"
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return db.execute("SELECT user, message FROM messages ORDER BY id").fetchall()


def _body(message="the greenhouse report is ready", msg_id="kestrel-0000000001", **kw):
    return dict({"message": message, "msg_id": msg_id, "sent_at": time.time()}, **kw)


class TestTellHome(HiveConsoleCase):
    def setUp(self):
        super().setUp()
        self.home = self.cousin("wren", port=None, operator="Priya",
                                extra='\n[agent]\nrunner = "sdk"\n')
        self.other = self.cousin("sam", port=None, extra='\n[agent]\nrunner = "sdk"\n')

    def test_the_node_reaches_the_home_cousin_as_itself(self):
        self.enable(home_cousin="wren")
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",), name="Kestrel")
        status, body = self.hive_json("POST", "/hive/tell-home", token,
                                      _body(user="Priya", to="sam"))    # both ignored
        self.assertEqual(status, 200, body)
        self.assertEqual(body["to"], "wren")
        self.assertEqual(_messages(self.home), [("Kestrel", "the greenhouse report is ready")])
        self.assertEqual(_messages(self.other), [])

    def test_a_node_cannot_rename_itself_at_checkin(self):
        """Ruling P10a-1 (review C1): the node checks in as the operator, as
        a local cousin, or with a newline that would forge a second line;
        its message still lands under the name the operator minted."""
        self.enable(home_cousin="wren")
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",), name="Kestrel")
        for i, name in enumerate(("Priya", "Sam", "Kestrel\n[now] (Chat Priya")):
            status, _ = self.hive_json("POST", "/hive/checkin", token,
                                       {"port": 8210, "name": name})
            self.assertEqual(status, 200)
            status, body = self.hive_json("POST", "/hive/tell-home", token,
                                          _body(message="note %d" % i,
                                                msg_id="kestrel-%010d" % i))
            self.assertEqual(status, 200, body)
        self.assertEqual({u for u, _m in _messages(self.home)}, {"Kestrel"})

    def test_a_minted_name_that_is_the_operators_is_refused(self):
        self.enable(home_cousin="wren")
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",), name="Priya")
        status, body = self.hive_json("POST", "/hive/tell-home", token, _body())
        self.assertEqual(status, 403, body)
        self.assertEqual(_messages(self.home), [])

    def test_a_replay_is_refused(self):
        self.enable(home_cousin="wren")
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",))
        self.assertEqual(self.hive("POST", "/hive/tell-home", token, _body())[0], 200)
        status, body = self.hive_json("POST", "/hive/tell-home", token, _body())
        self.assertEqual(status, 409, body)
        self.assertEqual(len(_messages(self.home)), 1)

    def test_no_token_a_revoked_one_or_a_console_session_is_refused(self):
        self.enable(home_cousin="wren")
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",))
        self.store().revoke("kestrel")
        self.assertEqual(self.hive("POST", "/hive/tell-home", None, _body())[0], 401)
        self.assertEqual(self.hive("POST", "/hive/tell-home", token, _body())[0], 401)
        self.assertEqual(self.post("/hive/tell-home", _body())[0], 401)   # the operator's session
        self.assertEqual(_messages(self.home), [])

    def test_without_a_home_cousin_the_route_is_absent(self):
        self.enable()
        self.serve()
        token = self.store().mint_token("kestrel", scope=("own",))
        status, body = self.hive_json("POST", "/hive/tell-home", token, _body())
        self.assertEqual(status, 404, body)
        self.assertIn("home_cousin", body["error"])

    def test_a_bad_home_cousin_is_a_config_error(self):
        (self.root / "config" / "hive.toml").write_text(
            'enabled = true\npublic_url = "http://192.0.2.10:8600"\nhome_cousin = "../etc"\n')
        with self.assertRaises(hive_lib.HiveConfigError):
            hive_lib.hive_config(self.root)


class TestTheNode(unittest.TestCase):
    """templates/hive-node: TELL_HOME=1 sends [tell-home: ...] to the queen
    with the node's own token, a msg_id and a sent_at."""

    def _node(self):
        import importlib.util
        path = (pathlib.Path(__file__).resolve().parents[2] / "templates" / "hive-node"
                / "cousin_node.py")
        spec = importlib.util.spec_from_file_location("cousin_node_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_tell_home_goes_to_the_queen_with_the_token(self):
        node = self._node()
        config = node.NodeConfig({"COUSIN_SLUG": "kestrel", "HIVE_TOKEN": "hive_x",
                                  "QUEEN_URL": "http://192.0.2.10:8600", "TELL_HOME": "1"})
        self.assertTrue(config.tell_home)
        calls = []
        hive = node.Hive(config.queen_url, config.token)
        with mock.patch.object(hive, "_call", side_effect=lambda path, **kw: calls.append(
                (path, kw)) or {"ok": True}):
            self.assertTrue(hive.tell_home("ready", msg_id="kestrel-abc12345"))
        [(path, kw)] = calls
        self.assertEqual((path, kw["method"]), ("/hive/tell-home", "POST"))
        self.assertEqual(kw["body"]["msg_id"], "kestrel-abc12345")
        self.assertLess(abs(kw["body"]["sent_at"] - time.time()), 5)

    def test_a_dropped_tell_home_is_logged_and_never_retried(self):
        """Review round 2 on M4: the node sends a tell-home once; when the
        queen does not take it, or no home is configured, the log says so."""
        node = self._node()
        for env in ({"COUSIN_SLUG": "kestrel", "HIVE_TOKEN": "hive_x",
                     "QUEEN_URL": "http://192.0.2.10:8600", "TELL_HOME": "1"},
                    {"COUSIN_SLUG": "kestrel", "HIVE_TOKEN": "hive_x"}):
            config = node.NodeConfig(env)
            hive = node.Hive(config.queen_url, config.token)
            logged, calls = [], []
            brain = node.Brain(config, hive, None, log=logged.append)
            with mock.patch.object(hive, "_call", side_effect=lambda path, **kw: calls.append(
                    path)):
                self.assertFalse(brain._tell_home("ready"))
            self.assertLessEqual(len(calls), 1, env)
            self.assertEqual(len(logged), 1, env)
            self.assertIn("tell-home dropped", logged[0])

    def test_the_archive_env_carries_tell_home(self):
        env = spawn_node.render_node_env(slug="kestrel", name="Kestrel", port=8210,
                                         queen_url="http://192.0.2.10:8600", token="hive_x",
                                         home_chat=None, agent_cmd="", tell_home=True)
        self.assertIn("TELL_HOME=1", env)


if __name__ == "__main__":
    unittest.main()
