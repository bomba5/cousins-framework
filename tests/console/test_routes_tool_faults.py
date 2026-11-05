"""GET /api/tool-faults (docs/reference/console-api.md, "Tool faults"): the
rows each cousin's runner logged, across the host's cousins, read-only."""
import unittest

from cousin_lib.runner import tool_faults
from tests.console._harness import ConsoleCase


class TestToolFaultsRoute(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.wren = self.cousin("wren")
        self.kestrel = self.cousin("kestrel")
        tool_faults.Watch(self.wren, "sdk-w").finished("w1", True, "boom")
        tool_faults.Watch(self.kestrel, "sdk-k").finished(
            "k1", True, "<tool_use_error>Error: No such tool available: bash</tool_use_error>")
        self.serve()

    def test_every_cousin_with_its_counts_and_rows(self):
        status, body = self.get("/api/tool-faults")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["totals"], {"wren": {"failed": 1}, "kestrel": {"unknown_tool": 1}})
        self.assertEqual(sorted(r["id"] for r in body["rows"]), ["k1", "w1"])
        self.assertEqual(body["kinds"], list(tool_faults.KINDS))
        self.assertEqual((body["days"], body["matched"]), (7, 2))

    def test_one_cousin_and_one_kind(self):
        _, body = self.get("/api/tool-faults?cousin=wren")
        self.assertEqual(list(body["totals"]), ["wren"])
        _, body = self.get("/api/tool-faults?kind=unknown_tool")
        self.assertEqual([r["cousin"] for r in body["rows"]], ["kestrel"])

    def test_a_cousin_on_the_private_repository_is_never_shown(self):
        secret = self.cousin("kit", extra='\n[skills]\nrepos = ["private"]\n')
        tool_faults.Watch(secret, "sdk-s").finished("s1", True, "boom")
        _, body = self.get("/api/tool-faults")
        self.assertEqual(sorted(body["totals"]), ["kestrel", "wren"])
        self.assertEqual(body["cousins"], ["kestrel", "wren"])
        self.assertNotIn("s1", [r["id"] for r in body["rows"]])
        self.assertEqual(self.get("/api/tool-faults?cousin=kit")[0], 404)

    def test_a_query_out_of_range_is_refused(self):
        for query in ("days=0", "days=91", "limit=0", "limit=1001", "kind=nope", "days=x"):
            status, body = self.get("/api/tool-faults?" + query)
            self.assertEqual(status, 400, (query, body))
        status, _ = self.get("/api/tool-faults?cousin=nobody")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
