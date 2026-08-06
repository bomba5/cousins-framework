"""The web UI daemon: docs/ui-spec.md as executable contract.

The first test is the spec's opening sentence made executable: the
daemon holds no state, so a fresh process serves every view from the
stores alone. If a view needs something the daemon remembers, it has
become an owner - which the whole spec forbids.
"""
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from cousin_lib.config import CousinConfig
from cousin_lib.ui import UIServer, build_ui


class UICase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cousin(self, slug, name=None, port=8100, extra=""):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n[chat]\nport = %d\n%s'
            % (slug, name or slug.capitalize(), port, extra))
        return home

    def _serve(self, guard=None):
        server = build_ui(self.root, guard=guard)
        server.start()
        self.addCleanup(server.stop)
        return server

    def _get(self, server, path, addr=None):
        url = "http://127.0.0.1:%d%s" % (server.port, path)
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def _json(self, server, path):
        status, _, body = self._get(server, path)
        return status, json.loads(body)

    def _post(self, server, path, payload):
        url = "http://127.0.0.1:%d%s" % (server.port, path)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())


class TestKill9Property(UICase):
    def test_a_fresh_daemon_serves_the_fleet_from_the_filesystem(self):
        # The whole spec in one test: the daemon holds nothing, so a
        # brand-new process renders the registry that lives on disk.
        self._cousin("wren")
        self._cousin("toki", port=8101)
        server = self._serve()
        status, body = self._json(server, "/api/cousins")
        self.assertEqual(status, 200)
        slugs = {c["slug"] for c in body["cousins"]}
        self.assertEqual(slugs, {"wren", "toki"})

    def test_a_cousin_added_after_boot_appears_without_restart(self):
        # No cached registry: the view reads the filesystem each call,
        # so it cannot go stale against a cousin created moments ago.
        server = self._serve()
        _, body = self._json(server, "/api/cousins")
        self.assertEqual(body["cousins"], [])
        self._cousin("wren")
        _, body = self._json(server, "/api/cousins")
        self.assertEqual([c["slug"] for c in body["cousins"]], ["wren"])


class TestHealthAndStatic(UICase):
    def test_health_is_json_no_store(self):
        server = self._serve()
        status, headers, body = self._get(server, "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_index_is_served_and_traversal_is_blocked(self):
        server = self._serve()
        status, _, body = self._get(server, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body.lower())
        status, _, _ = self._get(server, "/../config/whatever")
        self.assertNotEqual(status, 200)


class TestGuardFirst(UICase):
    def test_denied_address_is_403_on_every_route(self):
        server = self._serve(guard=lambda addr: False)
        for path in ("/api/health", "/api/cousins", "/"):
            status, _, _ = self._get(server, path)
            self.assertEqual(status, 403, path)


class TestStoreProjections(UICase):
    def test_jobs_view_reads_the_jobs_store(self):
        # The daemon reads jobs.db - the module's own store - so the
        # view survives a UI restart because the UI never held it.
        from cousin_lib.jobs import register_job
        self._cousin("wren")
        with mock.patch.dict(os.environ,
                             {"COUSIN_HOME": str(self.root / "cousins"
                                                 / "wren")}):
            register_job(kind="subagent", title="map the tree",
                         spawned_by="wren")
        server = self._serve()
        status, body = self._json(server, "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual([j["title"] for j in body["jobs"]],
                         ["map the tree"])

    def test_loops_view_reports_the_daemon_status(self):
        # The UI reads the loops daemon's state; a never-run daemon is
        # named, not hidden - the loud-absence contract carried through
        # to the display layer.
        self._cousin("wren")
        server = self._serve()
        status, body = self._json(server, "/api/loops")
        self.assertEqual(status, 200)
        self.assertFalse(body["daemon"]["ok"])
        self.assertIn("never run", body["daemon"]["message"])

    def test_requests_view_reads_the_loop_request_store(self):
        from cousin_lib.loops import submit_request
        self._cousin("wren")
        submit_request("fire", cousin="wren", payload={"loop": "x"})
        server = self._serve()
        _, body = self._json(server, "/api/loops/requests")
        self.assertEqual([r["status"] for r in body["requests"]],
                         ["pending"])


class TestCommandsThroughStores(UICase):
    def test_fire_command_writes_a_request_row_not_ui_state(self):
        # A command is a request the loops daemon consumes - the UI
        # writes through the same store a CLI would, holding nothing.
        from cousin_lib.loops import list_requests
        self._cousin("wren")
        server = self._serve()
        status, body = self._post(
            server, "/api/loops/fire",
            {"cousin": "wren", "loop": "report"})
        self.assertEqual(status, 200)
        rows = list_requests()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "fire")
        self.assertEqual(rows[0]["status"], "pending")

    def test_delete_is_guarded_like_every_other_route(self):
        # The source left cousin destruction ungated; here DELETE
        # passes the same guard as everything else.
        self._cousin("wren")
        server = self._serve(guard=lambda addr: False)
        url = "http://127.0.0.1:%d/api/cousins/wren" % server.port
        req = urllib.request.Request(url, method="DELETE")
        try:
            status = urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as err:
            status = err.code
        self.assertEqual(status, 403)

    def test_no_side_effecting_get(self):
        # A GET never mutates: the fire path must not be a GET route at
        # all. Asserting 404 (unrouted) distinguishes that from a fire
        # handler that happens to 400 on a GET's empty body - the
        # latter would leave a routed side-effecting GET undetected.
        self._cousin("wren")
        server = self._serve()
        from cousin_lib.loops import list_requests
        status, _, _ = self._get(
            server, "/api/loops/fire?cousin=wren&loop=report")
        self.assertEqual(status, 404)
        self.assertEqual(list_requests(), [])


class TestCli(UICase):
    def test_main_needs_a_framework_root(self):
        from cousin_lib.ui import ui_main
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ui_main(["--port", "0"]), 2)

    def test_root_accepts_the_same_flag_as_spawn(self):
        # cousin-spawn takes --root; cousin-ui must too, or the two
        # entry points disagree about how to be told the same fact and
        # the disagreement is discovered by failing, not by --help.
        self._cousin("wren")
        from cousin_lib.ui import build_ui_from_cli
        server = build_ui_from_cli(["--root", str(self.root),
                                    "--port", "0"])
        self.addCleanup(server.stop)
        server.start()
        status, body = self._json(server, "/api/cousins")
        self.assertEqual([c["slug"] for c in body["cousins"]], ["wren"])

    def test_flag_and_env_agree_flag_wins(self):
        # Both channels work, and an explicit flag beats the
        # environment - the same precedence spawn documents.
        self._cousin("wren")
        other = self.root / "decoy-root"
        (other / "cousins").mkdir(parents=True)
        from cousin_lib.ui import build_ui_from_cli
        with mock.patch.dict(os.environ,
                             {"FRAMEWORK_ROOT": str(other)}):
            server = build_ui_from_cli(["--root", str(self.root),
                                        "--port", "0"])
        self.addCleanup(server.stop)
        server.start()
        _, body = self._json(server, "/api/cousins")
        self.assertEqual([c["slug"] for c in body["cousins"]], ["wren"])


if __name__ == "__main__":
    unittest.main()
