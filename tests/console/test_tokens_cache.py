"""The prompt-cache hit rate in the tokens view (master plan phase 5 task
6): cache_read / (cache_read + cache_creation + input), per cousin and per
day, from the usage the model reported (usage.db on the SDK lane, the
harness transcript on the tmux lane), measured not inferred. A result that
carried no usage is left out of the rate, never counted as a miss."""
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from cousin_lib import usage
from cousin_lib.console import tokens
from tests._hermetic import HermeticCase


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _usage(inp, read, creation, out=5):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": creation}


class Fleet(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.server = mock.Mock(root=self.root, state={})
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        p.start(); self.addCleanup(p.stop)

    def _cousin(self, slug, runner=""):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True)
        agent = '\n[agent]\nrunner = "%s"\n' % runner if runner else ""
        (home / "cousin.toml").write_text('[cousin]\nslug = "%s"\nname = "%s"\n%s'
                                          % (slug, slug.capitalize(), agent))
        return home


class TestSdkLane(Fleet):
    def test_a_day_of_known_usage_renders_the_exact_rate(self):
        home = self._cousin("wren", "sdk")
        for u in (_usage(10, 90, 0), _usage(20, 0, 80)):
            usage.record(home, client_id="c", session_id="s",
                         result={"usage": u, "total_cost_usd": 0.0}, lane="key")
        today = tokens.cache_days(self.server, home)[_today()]
        self.assertEqual((today["read"], today["creation"], today["input"]), (90, 80, 30))
        self.assertEqual(today["rate"], 90 / 200)

    def test_a_result_with_no_usage_is_left_out_not_a_miss(self):
        home = self._cousin("wren", "sdk")
        usage.record(home, client_id="c", session_id="s",
                     result={"usage": _usage(10, 90, 0), "total_cost_usd": 0.0}, lane="key")
        usage.record(home, client_id="c", session_id="s",
                     result={"usage": None, "total_cost_usd": 0.0}, lane="key")
        self.assertEqual(tokens.cache_days(self.server, home)[_today()]["rate"], 0.9)

    def test_no_usage_at_all_is_no_rate(self):
        home = self._cousin("wren", "sdk")
        self.assertEqual(tokens.cache(self.server, home)["rate"], None)
        self.assertEqual(tokens.cache(self.server, home)["days"][-1]["rate"], None)


class TestTmuxLane(Fleet):
    def test_the_transcripts_usage_gives_the_same_rate(self):
        (self.root / "config" / "harness.toml").write_text(
            'transcripts_dir = "{home}/transcripts"\n')
        home = self._cousin("testa")
        (home / "transcripts").mkdir()
        lines = [{"timestamp": _today() + "T10:00:00Z",
                  "message": {"id": "m1", "usage": _usage(10, 90, 0)}},
                 {"timestamp": _today() + "T10:01:00Z",
                  "message": {"id": "m2", "usage": _usage(20, 0, 80)}},
                 {"timestamp": _today() + "T10:02:00Z", "message": {"role": "user"}}]
        (home / "transcripts" / "one.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines))
        self.assertEqual(tokens.cache_days(self.server, home)[_today()]["rate"], 90 / 200)
        self.assertEqual(tokens.cache(self.server, home)["rate"], 90 / 200)


class TestRoute(Fleet):
    def test_the_route_carries_the_rate_beside_an_unchanged_series(self):
        from types import SimpleNamespace
        from cousin_lib.console import router, routes_fleet
        router.clear()
        routes_fleet.register()
        home = self._cousin("wren", "sdk")
        usage.record(home, client_id="c", session_id="s",
                     result={"usage": _usage(10, 90, 0), "total_cost_usd": 0.0}, lane="key")
        status, body = router.dispatch("GET", "/api/tokens",
                                       req=SimpleNamespace(server=self.server, query={},
                                                           body={}))
        self.assertEqual(status, 200)
        row = body["cousins"][0]
        self.assertEqual(set(row["series"][-1]), {"day", "total", "output"})
        self.assertEqual(row["cache"]["rate"], 0.9)
        self.assertEqual(len(row["cache"]["days"]), tokens.SERIES_DAYS)


if __name__ == "__main__":
    unittest.main()
