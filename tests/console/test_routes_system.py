"""WP-F, the System view's routes (console/routes_system.py): the
supervisor through its own request API (a stub on the socket), per-cousin
one-shot schedules, console users with write-only passwords, backup as a
LongOp per cousin into a checked destination, and the install config
editors, each checked by its own loader before anything is written."""
import json
import os
import stat
import tempfile
import time
import tomllib
from pathlib import Path
from unittest import mock

from cousin_lib.console import auth
from tests._stub_supervisor import StubSupervisor
from tests.console._harness import ConsoleCase

RUNNER = '\n[agent]\nrunner = "fake"\n'


def wait_op(case, slug, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body = case.get("/api/cousins/%s/op" % slug)
        op = body.get("op")
        if op and op["status"] != "running":
            return op
        time.sleep(0.05)
    raise AssertionError("op on %s did not finish" % slug)


# ---- the supervisor ---------------------------------------------------------

class Supervisor(ConsoleCase):
    def stub(self, **answers):
        stub = StubSupervisor(self.root, answers).start()
        self.addCleanup(stub.close)
        return stub

    def test_status_lists_every_child_with_what_may_be_done_to_it(self):
        children = {"console": {"state": "running", "pid": 10, "restarts": 0, "since": "t",
                                "reason": None, "last_exit": None},
                    "loops": {"state": "running", "pid": 11, "restarts": 1, "since": "t",
                              "reason": None, "last_exit": 1},
                    "runner:wren": {"state": "failing", "pid": None, "restarts": 5,
                                    "since": "t", "reason": "crashed", "last_exit": 3},
                    "telegram:wren": {"state": "running", "pid": 12, "restarts": 0,
                                      "since": "t", "reason": None, "last_exit": None}}
        self.stub(status={"ok": True, "pid": 1, "started": "t", "children": children})
        self.serve()
        status, body = self.get("/api/system/supervisor")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["running"])
        rows = {r["name"]: r for r in body["children"]}
        self.assertEqual(rows["runner:wren"]["reason"], "crashed")
        self.assertEqual(rows["runner:wren"]["kind"], "runner")
        self.assertFalse(rows["console"]["actions"]["stop"])
        self.assertTrue(rows["console"]["actions"]["restart"])
        self.assertEqual(rows["loops"]["actions"]["confirm"], "loops")
        self.assertFalse(rows["telegram:wren"]["actions"]["start"])

    def test_no_supervisor_is_said_plainly(self):
        self.serve()
        status, body = self.get("/api/system/supervisor")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["running"])
        self.assertIn("no cousin-supervisor", body["reason"])

    def test_the_console_is_never_stopped_from_its_own_page(self):
        stub = self.stub()
        self.serve()
        status, body = self.post("/api/system/supervisor/stop", {"child": "console"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["restart_route"], "/api/admin/restart/framework")
        self.assertEqual(stub.requests, [])

    def test_stopping_the_loops_daemon_needs_its_confirmation(self):
        stub = self.stub(stop={"ok": True, "name": "loops", "state": "stopping"})
        self.serve()
        status, body = self.post("/api/system/supervisor/stop", {"child": "loops"})
        self.assertEqual(status, 400, body)
        self.assertEqual(stub.requests, [])
        status, body = self.post("/api/system/supervisor/stop",
                                 {"child": "loops", "confirm": "loops"})
        self.assertEqual(status, 200, body)
        req = stub.requests[0]
        self.assertEqual((req["op"], req["name"], req["wait"]), ("stop", "loops", False))
        self.assertIn("console", req["by"])

    def test_a_runner_child_is_started_by_slug(self):
        self.cousin("wren", extra=RUNNER)
        stub = self.stub()
        self.serve()
        status, body = self.post("/api/system/supervisor/start", {"child": "runner:wren"})
        self.assertEqual(status, 200, body)
        self.assertEqual(stub.ops(), [("start", "wren")])

    def test_an_unknown_runner_and_a_bridge_are_refused(self):
        stub = self.stub()
        self.serve()
        status, _ = self.post("/api/system/supervisor/start", {"child": "runner:ghost"})
        self.assertEqual(status, 404)
        status, body = self.post("/api/system/supervisor/stop", {"child": "telegram:wren"})
        self.assertEqual(status, 400, body)
        self.assertEqual(stub.requests, [])

    def test_a_runner_busy_with_an_op_is_not_stopped_underneath_it(self):
        from cousin_lib.console import longop
        self.cousin("wren", extra=RUNNER)
        stub = self.stub()
        server = self.serve()
        hold = longop.exclusive(server, "wren", "migrate")
        self.addCleanup(hold.release)
        status, body = self.post("/api/system/supervisor/stop", {"child": "runner:wren"})
        self.assertEqual(status, 409, body)
        self.assertEqual(stub.requests, [])

    def test_a_refusal_is_passed_on(self):
        self.cousin("wren", extra=RUNNER)
        self.stub(start={"ok": False, "error": "runner:wren is still stopping"})
        self.serve()
        status, body = self.post("/api/system/supervisor/start", {"child": "runner:wren"})
        self.assertEqual(status, 409, body)
        self.assertIn("still stopping", body["error"])

    def test_reload_asks_the_supervisor(self):
        stub = self.stub(reload={"ok": True, "added": ["runner:wren"], "removed": []})
        self.serve()
        status, body = self.post("/api/system/supervisor/reload")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["added"], ["runner:wren"])
        self.assertEqual([r["op"] for r in stub.requests], ["reload"])

    def test_the_state_changing_routes_are_post_only(self):
        self.serve()
        for path in ("/api/system/supervisor/start", "/api/system/supervisor/stop",
                     "/api/system/supervisor/reload", "/api/system/users",
                     "/api/system/backup", "/api/system/law"):
            status, _ = self.get(path) if path != "/api/system/users" else (405, None)
            self.assertEqual(status, 405, path)


# ---- schedules --------------------------------------------------------------

class Schedules(ConsoleCase):
    def test_add_list_and_cancel_a_one_shot(self):
        self.cousin("wren")
        self.serve()
        status, body = self.post("/api/cousins/wren/schedules",
                                 {"when": "in 30m", "prompt": "check the backup"})
        self.assertEqual(status, 201, body)
        job = body["schedule"]["id"]
        _, body = self.get("/api/cousins/wren/schedules")
        self.assertEqual([r["prompt"] for r in body["schedules"]], ["check the backup"])
        _, body = self.get("/api/system/schedules")
        self.assertEqual([r["cousin"] for r in body["schedules"]], ["wren"])
        status, body = self.post("/api/cousins/wren/schedules/%d/cancel" % job)
        self.assertEqual(status, 200, body)
        _, body = self.get("/api/cousins/wren/schedules")
        self.assertEqual(body["schedules"], [])
        _, body = self.get("/api/cousins/wren/schedules?all=1")
        self.assertEqual([r["status"] for r in body["schedules"]], ["cancelled"])

    def test_a_bad_time_or_empty_prompt_is_400(self):
        self.cousin("wren")
        self.serve()
        for payload in ({"when": "whenever", "prompt": "x"}, {"when": "in 5m", "prompt": " "},
                        {"when": "2001-01-01", "prompt": "x"}, {"prompt": "x"}):
            status, _ = self.post("/api/cousins/wren/schedules", payload)
            self.assertEqual(status, 400, payload)

    def test_another_cousins_job_is_not_cancelled(self):
        self.cousin("wren")
        self.cousin("robin")
        self.serve()
        _, body = self.post("/api/cousins/wren/schedules", {"when": "in 5m", "prompt": "x"})
        status, _ = self.post("/api/cousins/robin/schedules/%d/cancel" % body["schedule"]["id"])
        self.assertEqual(status, 404)

    def test_an_unknown_cousin_is_404(self):
        self.serve()
        status, _ = self.get("/api/cousins/ghost/schedules")
        self.assertEqual(status, 404)


# ---- console users ----------------------------------------------------------

class Users(ConsoleCase):
    def users_file(self):
        return self.root / "config" / "console-users.json"

    def login(self, name, password):
        status, body = self.post("/api/auth/login", {"user": name, "password": password})
        self.assertEqual(status, 200, body)

    def setup_users(self, *names):
        users = auth.Users(self.users_file())
        for name in names:
            users.set_password(name, "pw-%s-long" % name)
        self.serve()
        self.login(names[0], "pw-%s-long" % names[0])

    def test_add_reset_and_remove_with_the_password_never_returned(self):
        self.setup_users("ana")
        status, body = self.post("/api/system/users", {"name": "bo", "password": "secret-one"})
        self.assertEqual(status, 201, body)
        self.assertNotIn("secret-one", json.dumps(body))
        self.assertTrue(auth.Users(self.users_file()).verify("bo", "secret-one"))
        status, body = self.post("/api/system/users/bo/password", {"password": "secret-two"})
        self.assertEqual(status, 200, body)
        self.assertNotIn("secret-two", json.dumps(body))
        self.assertTrue(auth.Users(self.users_file()).verify("bo", "secret-two"))
        _, body = self.get("/api/system/users")
        self.assertEqual(body["users"], ["ana", "bo"])
        self.assertEqual(body["me"], "ana")
        self.assertNotIn("hash", json.dumps(body))
        status, body = self.post("/api/system/users/bo/remove", {})
        self.assertEqual(status, 400, body)
        status, body = self.post("/api/system/users/bo/remove", {"confirm": "bo"})
        self.assertEqual(status, 200, body)
        self.assertEqual(auth.Users(self.users_file()).names(), ["ana"])

    def test_yourself_and_the_last_user_are_never_removed(self):
        self.setup_users("ana")
        status, body = self.post("/api/system/users/ana/remove", {"confirm": "ana"})
        self.assertEqual(status, 400, body)
        self.assertIn("logged in as", body["error"])
        self.assertEqual(auth.Users(self.users_file()).names(), ["ana"])
        with self.assertRaises(ValueError):
            auth.Users(self.users_file()).remove("ana")

    def test_your_own_reset_goes_through_change_password(self):
        self.setup_users("ana", "bo")
        status, body = self.post("/api/system/users/ana/password", {"password": "x" * 12})
        self.assertEqual(status, 400, body)
        self.assertTrue(auth.Users(self.users_file()).verify("ana", "pw-ana-long"))

    def test_a_short_password_a_bad_name_and_a_duplicate_are_refused(self):
        self.setup_users("ana")
        for payload, code in (({"name": "bo", "password": "short"}, 400),
                              ({"name": "b o", "password": "long-enough"}, 400),
                              ({"name": "ana", "password": "long-enough"}, 409)):
            status, _ = self.post("/api/system/users", payload)
            self.assertEqual(status, code, payload)

    def test_a_removed_users_session_ends(self):
        self.setup_users("ana", "bo")
        self.post("/api/system/users/bo/remove", {"confirm": "bo"})
        self.jar.clear()
        status, _ = self.post("/api/auth/login", {"user": "bo", "password": "pw-bo-long"})
        self.assertEqual(status, 401)

    def test_the_first_user_logs_its_maker_in(self):
        self.serve()
        status, body = self.post("/api/system/users", {"name": "ana", "password": "first-pw-1"})
        self.assertEqual(status, 201, body)
        self.assertTrue(body["logged_in"])
        _, me = self.get("/api/auth/me")
        self.assertEqual(me["user"], "ana")
        self.assertEqual(stat.S_IMODE(self.users_file().stat().st_mode), 0o600)

    def test_login_is_required_once_users_exist(self):
        auth.Users(self.users_file()).set_password("ana", "pw-ana-long")
        self.serve()
        status, _ = self.post("/api/system/users", {"name": "eve", "password": "long-enough"})
        self.assertEqual(status, 401)


# ---- backup -----------------------------------------------------------------

class Backup(ConsoleCase):
    def dest(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_back_up_now_runs_one_op_per_cousin_and_a_job_row(self):
        home = self.cousin("wren")
        (home / "MEMORY.md").write_text("# memory\n")
        self.cousin("robin")
        dest = self.dest()
        self.serve()
        status, body = self.post("/api/system/backup", {"dest": dest})
        self.assertEqual(status, 202, body)
        self.assertEqual(sorted(body["ops"]), ["robin", "wren"])
        op = wait_op(self, "wren")
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(op["kind"], "backup")
        snap = Path(op["result"]["snapshot"])
        self.assertEqual(snap.parent, Path(dest) / "wren")
        self.assertEqual((snap / "MEMORY.md").read_text(), "# memory\n")
        wait_op(self, "robin")
        from cousin_lib import jobs
        rows = jobs.list_jobs()
        self.assertEqual(sorted((r["spawned_by"], r["kind"], r["status"]) for r in rows),
                         [("robin", "backup", "done"), ("wren", "backup", "done")])

    def test_the_destination_is_checked(self):
        self.cousin("wren")
        self.serve()
        (self.root / ".secrets").mkdir()
        link = Path(self.dest()) / "sneaky"
        os.symlink(self.root / "config", link)
        for dest in ("relative/dir", "", str(self.root / "cousins"),
                     str(self.root / "cousins" / "wren" / "data"),
                     str(self.root / "config"), str(self.root / ".secrets"), str(link),
                     str(self.root / "no-such-dir")):
            status, body = self.post("/api/system/backup", {"dest": dest, "slugs": ["wren"]})
            self.assertEqual(status, 400, (dest, body))
        self.assertEqual(os.listdir(self.root / "config"), [])

    def test_a_snapshot_dir_may_not_land_in_the_live_root(self):
        self.cousin("config")  # a slug named like a protected directory
        self.serve()
        status, body = self.post("/api/system/backup",
                                 {"dest": str(self.root), "slugs": ["config"]})
        self.assertEqual(status, 400, body)

    def test_an_unwritable_destination_is_refused(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        self.cousin("wren")
        dest = self.dest()
        os.chmod(dest, 0o500)
        self.addCleanup(os.chmod, dest, 0o700)
        self.serve()
        status, body = self.post("/api/system/backup", {"dest": dest})
        self.assertEqual(status, 400, body)
        self.assertIn("not writable", body["error"])

    def test_a_busy_cousin_is_reported_not_backed_up(self):
        from cousin_lib.console import longop
        self.cousin("wren")
        server = self.serve()
        hold = longop.exclusive(server, "wren", "migrate")
        self.addCleanup(hold.release)
        status, body = self.post("/api/system/backup", {"dest": self.dest()})
        self.assertEqual(status, 409, body)
        self.assertIn("wren", body["busy"])


# ---- install config ---------------------------------------------------------

class TomlConfig(ConsoleCase):
    def cfg(self, name):
        return self.root / "config" / name

    def change(self, name, changes, removes=None):
        body = {"changes": [dict(table=t, key=k, value=v) for t, k, v in changes]}
        if removes:
            body["remove_tables"] = removes
        return self.post("/api/system/config/%s" % name, body)

    def test_media_kind_is_written_and_read_back_and_removed(self):
        self.serve()
        status, body = self.change("media", [("image", "url", "http://192.0.2.1/img"),
                                             ("image", "model", "m1"),
                                             ("image", "timeout_s", 60)])
        self.assertEqual(status, 200, body)
        data = tomllib.loads(self.cfg("media.toml").read_text())
        self.assertEqual(data["image"], {"url": "http://192.0.2.1/img", "model": "m1",
                                         "timeout_s": 60})
        _, body = self.get("/api/system/config")
        self.assertEqual(body["files"]["media"]["kinds"]["image"]["model"], "m1")
        self.assertIsNone(body["files"]["media"]["kinds"]["voice"])
        status, body = self.change("media", [], removes=["image"])
        self.assertEqual(status, 200, body)
        self.assertNotIn("image", tomllib.loads(self.cfg("media.toml").read_text()))

    def test_a_path_field_is_not_free_text(self):
        self.serve()
        status, body = self.change("media", [("image", "key_file", "/etc/passwd")])
        self.assertEqual(status, 400, body)
        status, body = self.change("peers", [("peers.kestrel", "token_file", "x")])
        self.assertEqual(status, 400, body)
        self.assertFalse(self.cfg("media.toml").exists())

    def test_bad_values_are_refused_and_nothing_written(self):
        self.serve()
        for name, change in (("media", ("image", "url", "ftp://x")),
                             ("media", ("image", "timeout_s", -1)),
                             ("media", ("sound", "url", "http://h/x")),
                             ("embedding", ("recall", "min_score", 3)),
                             ("hive", ("", "enabled", "yes"))):
            status, body = self.change(name, [change])
            self.assertEqual(status, 400, (name, change, body))
        self.assertEqual(os.listdir(self.root / "config"), [])

    def test_embedding_needs_a_url_by_its_own_loader(self):
        self.serve()
        status, body = self.change("embedding", [("", "model", "nomic")])
        self.assertEqual(status, 400, body)
        self.assertIn("url", body["error"])
        status, body = self.change("embedding", [("", "url", "http://127.0.0.1:11434/api/e"),
                                                 ("", "model", "nomic"),
                                                 ("recall", "min_score", 0.5)])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["file"]["values"]["recall.min_score"], 0.5)

    def test_hive_is_checked_by_hive_config(self):
        self.cfg("hive.toml").write_text("# the queen\nenabled = false\n")
        self.serve()
        status, body = self.change("hive", [("", "enabled", True)])
        self.assertEqual(status, 400, body)
        self.assertIn("public_url", body["error"])
        self.assertNotIn("console-config-check", body["error"])
        status, body = self.change("hive", [("", "enabled", True),
                                            ("", "public_url", "http://192.0.2.10:8600")])
        self.assertEqual(status, 200, body)
        text = self.cfg("hive.toml").read_text()
        self.assertTrue(text.startswith("# the queen\n"))
        self.assertEqual(tomllib.loads(text)["public_url"], "http://192.0.2.10:8600")

    def test_peers_add_edit_and_remove(self):
        self.cousin("wren")
        self.serve()
        status, body = self.change("peers", [("peers.kestrel", "url", "http://127.0.0.1:8085"),
                                             ("peers.kestrel", "reach", ["wren", "ghost"])])
        self.assertEqual(status, 200, body)
        peer = body["file"]["peers"]["kestrel"]
        self.assertEqual(peer["reach"], ["wren", "ghost"])
        self.assertEqual(peer["unknown_reach"], ["ghost"])
        status, body = self.change("peers", [("peers.kestrel", "send_path", "no-slash")])
        self.assertEqual(status, 400, body)
        status, body = self.change("peers", [], removes=["peers.kestrel"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["file"]["peers"], {})

    def test_a_malformed_file_is_refused_not_overwritten(self):
        self.cfg("hive.toml").write_text("enabled = [\n")
        self.serve()
        status, body = self.change("hive", [("", "enabled", False)])
        self.assertEqual(status, 400, body)
        self.assertEqual(self.cfg("hive.toml").read_text(), "enabled = [\n")


class Secrets(ConsoleCase):
    def test_a_media_key_is_write_only_and_lands_at_its_own_path(self):
        self.serve()
        status, body = self.post("/api/system/config/media/image/secret",
                                 {"value": "sk-abcdefghijklmnop1234"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["secret"], {"set": True, "last4": "1234", "error": None})
        self.assertNotIn("abcdefghijklmnop", json.dumps(body))
        key = self.root / "config" / "media-keys" / "image.key"
        self.assertEqual(key.read_text(), "sk-abcdefghijklmnop1234\n")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(tomllib.loads((self.root / "config" / "media.toml").read_text()),
                         {"image": {"key_file": "config/media-keys/image.key"}})
        _, cfg = self.get("/api/system/config")
        self.assertNotIn("abcdefghijklmnop", json.dumps(cfg))
        self.assertTrue(cfg["files"]["media"]["kinds"]["image"]["key"]["set"])
        status, body = self.post("/api/system/config/media/image/secret/clear")
        self.assertEqual(status, 200, body)
        self.assertFalse(key.exists())
        self.assertEqual(tomllib.loads((self.root / "config" / "media.toml").read_text()),
                         {"image": {}})

    def test_a_peer_token_needs_the_peer_and_sets_its_file(self):
        self.serve()
        status, _ = self.post("/api/system/config/peers/kestrel/secret",
                              {"value": "t" * 32, "which": "inbound"})
        self.assertEqual(status, 404)
        self.assertFalse((self.root / "config" / "peer-tokens").exists())
        (self.root / "config" / "external-peers.toml").write_text(
            '[peers.kestrel]\nurl = "http://127.0.0.1:8085"\nreach = []\n')
        status, body = self.post("/api/system/config/peers/kestrel/secret",
                                 {"value": "t" * 32, "which": "inbound"})
        self.assertEqual(status, 200, body)
        data = tomllib.loads((self.root / "config" / "external-peers.toml").read_text())
        self.assertEqual(data["peers"]["kestrel"]["inbound_token_file"],
                         "config/peer-tokens/kestrel.inbound.token")
        from cousin_lib import chat
        self.assertEqual(chat.read_secret(self.root, "config/peer-tokens/kestrel.inbound.token",
                                          "token"), "t" * 32)

    def test_a_bad_secret_is_refused(self):
        self.serve()
        for value in ("", "two words", "a\nb", None):
            status, _ = self.post("/api/system/config/media/image/secret", {"value": value})
            self.assertEqual(status, 400, value)
        self.assertFalse((self.root / "config" / "media.toml").exists())


class TextConfig(ConsoleCase):
    def test_law_is_written_backed_up_and_refused_when_stale(self):
        law = self.root / "config" / "law.md"
        law.write_text("# old law\n")
        self.serve()
        _, cfg = self.get("/api/system/config")
        sha = cfg["files"]["law"]["sha"]
        status, body = self.post("/api/system/law", {"content": "# new law\n", "base_sha": sha})
        self.assertEqual(status, 200, body)
        self.assertEqual(law.read_text(), "# new law\n")
        self.assertEqual((self.root / body["backup"]).read_text(), "# old law\n")
        status, body = self.post("/api/system/law", {"content": "# newer\n", "base_sha": sha})
        self.assertEqual(status, 409, body)
        self.assertEqual(law.read_text(), "# new law\n")

    def test_the_outbound_filter_is_checked_against_what_its_loader_reads(self):
        self.serve()
        for content in ("not json", '{"terms": "x"}', '{"protected": [""]}',
                        '{"surfaces": {"chat": {"add": [1]}}}', '{"term": []}'):
            status, body = self.post("/api/system/outbound-filter",
                                     {"content": content, "base_sha": ""})
            self.assertEqual(status, 400, (content, body))
        good = '{"terms": ["alpha"], "protected": ["beta"], "trusted_peers": [],' \
               ' "surfaces": {"chat": {"add": ["gamma"]}}}'
        status, body = self.post("/api/system/outbound-filter",
                                 {"content": good, "base_sha": ""})
        self.assertEqual(status, 200, body)
        from cousin_lib.outbound_filter import OutboundPolicy
        self.assertEqual(OutboundPolicy.load(self.root).scan("alpha gamma beta"),
                         ["alpha", "beta", "gamma"])

    def test_the_allowlist_keeps_the_caller_in_and_offers_the_restart(self):
        path = self.root / "config" / "net-allowlist.json"
        path.write_text('{"allow": ["100.64.0.0/10"], "note": "kept"}\n')
        self.serve()
        _, cfg = self.get("/api/system/config")
        allow = cfg["files"]["allowlist"]
        self.assertEqual(allow["allow"], ["100.64.0.0/10"])
        self.assertEqual(allow["restart"]["route"], "/api/admin/restart/framework")
        status, body = self.post("/api/system/allowlist",
                                 {"allow": ["100.64.0.0/10", "203.0.113.0/24"],
                                  "base_sha": allow["sha"]})
        self.assertEqual(status, 200, body)
        data = json.loads(path.read_text())
        self.assertEqual(data, {"allow": ["100.64.0.0/10", "203.0.113.0/24"], "note": "kept"})
        for allow_list in (["0.0.0.0/0"], ["10.0.0.1/8"], ["nonsense"], "x"):
            status, body = self.post("/api/system/allowlist", {"allow": allow_list})
            self.assertEqual(status, 400, (allow_list, body))

    def test_the_allowlist_never_locks_the_caller_out(self):
        from cousin_lib.server.netguard import NetGuard
        self.serve(guard=NetGuard(["100.64.0.0/10"]))
        with mock.patch("cousin_lib.server.netguard._DEFAULT_NETWORKS", []):
            status, body = self.post("/api/system/allowlist", {"allow": ["203.0.113.0/24"]})
        self.assertEqual(status, 400, body)
        self.assertIn("127.0.0.1", body["error"])
        self.assertFalse((self.root / "config" / "net-allowlist.json").exists())

    def test_the_command_files_are_shown_and_never_written(self):
        (self.root / "config" / "agent-cmd").write_text("agent --model {model}\n")
        self.serve()
        _, cfg = self.get("/api/system/config")
        self.assertEqual(cfg["files"]["commands"]["agent-cmd"]["content"],
                         "agent --model {model}\n")
        self.assertFalse(cfg["files"]["commands"]["worker-cmd"]["exists"])
        for name in ("agent-cmd", "commands"):
            status, _ = self.post("/api/system/config/%s" % name, {"changes": []})
            self.assertEqual(status, 404)


class AgentDefaults(ConsoleCase):
    HARNESS = ('# harness\ntranscripts_dir = "/x/{home_encoded}"\n\n'
               '[agent]\ndefault_model = "some-model"\ncommit_attribution = false\n')

    def test_each_value_names_its_source(self):
        (self.root / "config" / "harness.toml").write_text(self.HARNESS)
        self.serve()
        status, body = self.get("/api/system/agent-defaults")
        self.assertEqual(status, 200, body)
        v = body["values"]
        self.assertEqual(v["default_model"], {"value": "some-model",
                                              "source": "config/harness.toml [agent]"})
        self.assertEqual(v["commit_attribution"]["value"], False)
        self.assertEqual(v["commit_attribution"]["source"], "config/harness.toml [agent]")
        self.assertIsNone(v["default_effort"]["value"])
        self.assertTrue(v["default_effort"]["source"].startswith("unset"))
        self.assertIn("xhigh", body["choices"]["effort"])

    def test_an_absent_attribution_is_the_built_in_true(self):
        (self.root / "config" / "harness.toml").write_text("[agent]\n")
        self.serve()
        _, body = self.get("/api/system/agent-defaults")
        self.assertEqual(body["values"]["commit_attribution"]["value"], True)
        self.assertIn("built-in", body["values"]["commit_attribution"]["source"])

    def test_set_and_unset_keep_the_rest_of_the_file(self):
        path = self.root / "config" / "harness.toml"
        path.write_text(self.HARNESS)
        self.serve()
        status, body = self.post("/api/system/agent-defaults",
                                 {"default_effort": "high", "commit_attribution": True,
                                  "default_model": None})
        self.assertEqual(status, 200, body)
        data = tomllib.loads(path.read_text())
        self.assertEqual(data["agent"], {"commit_attribution": True, "default_effort": "high"})
        self.assertEqual(data["transcripts_dir"], "/x/{home_encoded}")
        self.assertTrue(path.read_text().startswith("# harness\n"))
        self.assertEqual(body["values"]["default_effort"]["source"],
                         "config/harness.toml [agent]")

    def test_bad_values_are_refused(self):
        path = self.root / "config" / "harness.toml"
        path.write_text(self.HARNESS)
        self.serve()
        for payload in ({"default_effort": "huge"}, {"commit_attribution": "false"},
                        {"default_model": "two words"}, {}):
            status, body = self.post("/api/system/agent-defaults", payload)
            self.assertEqual(status, 400, (payload, body))
        self.assertEqual(path.read_text(), self.HARNESS)

    def test_an_absent_harness_toml_is_not_created(self):
        self.serve()
        status, body = self.post("/api/system/agent-defaults", {"default_effort": "high"})
        self.assertEqual(status, 409, body)
        self.assertFalse((self.root / "config" / "harness.toml").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
