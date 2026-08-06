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


if __name__ == "__main__":
    unittest.main()
