"""usage: per-turn rows from cumulative per-client figures; both lanes in the view, once each."""
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from cousin_lib import usage
from tests._hermetic import HermeticCase

U = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
     "cache_creation_input_tokens": 0}
TODAY = datetime.now(timezone.utc).date().isoformat()


def _root(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    return pathlib.Path(tmp.name)


def _cousin(root, slug, runner):
    home = root / "cousins" / slug; (home / "data").mkdir(parents=True)
    agent = '\n[agent]\nrunner = "%s"\n' % runner if runner else ""
    (home / "cousin.toml").write_text('[cousin]\nslug = "%s"\nname = "%s"\n%s'
                                      % (slug, slug.capitalize(), agent))
    return home


def _result(cost, session="s-1"):
    return {"usage": dict(U), "total_cost_usd": cost, "session_id": session}


def _transcript_line(mid, inp, out):
    return json.dumps({"timestamp": TODAY + "T10:00:00Z",
                       "message": {"id": mid, "usage": {"input_tokens": inp,
                                                         "output_tokens": out}}}) + "\n"


class TestRecord(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = _cousin(_root(self), "wren", "sdk")

    def test_the_cost_is_the_per_turn_difference_per_client(self):
        a = usage.record(self.home, client_id="c-a", session_id="s-1", result=_result(0.0088), lane="login")
        b = usage.record(self.home, client_id="c-a", session_id="s-1", result=_result(0.0136), lane="login")
        c = usage.record(self.home, client_id="c-b", session_id="s-1", result=_result(0.0040), lane="login")
        self.assertAlmostEqual(a["cost_usd"], 0.0088)
        self.assertAlmostEqual(b["cost_usd"], 0.0048)
        self.assertAlmostEqual(c["cost_usd"], 0.0040)       # a new client starts over

    def test_a_restart_is_a_new_client_even_at_the_same_position(self):
        # the old process's first client and the new process's first client
        usage.record(self.home, client_id="proc1-first", session_id="s", result=_result(0.05), lane="key")
        row = usage.record(self.home, client_id="proc2-first", session_id="s", result=_result(0.01),
                           lane="key")
        self.assertAlmostEqual(row["cost_usd"], 0.01)       # not 0.01 - 0.05

    def test_login_lane_is_an_estimate_key_lane_is_not(self):
        self.assertTrue(usage.record(self.home, client_id="x", session_id="s", result=_result(0.01),
                                     lane="login")["estimate"])
        self.assertFalse(usage.record(self.home, client_id="y", session_id="s", result=_result(0.01),
                                      lane="key")["estimate"])

    def test_a_result_with_no_usage_is_a_zero_row_not_an_error(self):
        row = usage.record(self.home, client_id="x", session_id="s", result={"usage": None,
                           "total_cost_usd": None, "session_id": None}, lane="key")
        self.assertEqual(row["total"], 0); self.assertNotIn("error", row)

    def test_lane_for(self):
        self.assertEqual(usage.lane_for("ANTHROPIC_API_KEY"), "key")
        self.assertEqual(usage.lane_for("none"), "login")
        self.assertEqual(usage.lane_for(None), "unknown")

    def test_day_totals_sum_like_the_transcript_view(self):
        usage.record(self.home, client_id="x", session_id="s", result=_result(0.01), lane="key")
        usage.record(self.home, client_id="x", session_id="s", result=_result(0.03), lane="key")
        totals = usage.day_totals(self.home)
        self.assertEqual(totals[TODAY]["total"], 2 * 115)
        self.assertEqual(totals[TODAY]["output"], 10)
        self.assertAlmostEqual(totals[TODAY]["cost_usd"], 0.03)


class TestTokensView(HermeticCase):
    def _fleet(self, *, seam):
        root = _root(self)
        (root / "config").mkdir()
        if seam:
            (root / "config" / "harness.toml").write_text('transcripts_dir = "{home}/transcripts"\n')
        tmux = _cousin(root, "testa", "")
        sdk = _cousin(root, "wren", "sdk")
        for home, mid in ((tmux, "m-testa"), (sdk, "m-wren")):
            (home / "transcripts").mkdir()
            (home / "transcripts" / "one.jsonl").write_text(_transcript_line(mid, 10, 5))
        usage.record(sdk, client_id="x", session_id="s", result=_result(0.01), lane="key")
        return root, tmux, sdk

    def test_the_fleet_view_sums_both_lanes_once_each(self):
        from cousin_lib.console import tokens
        root, tmux, sdk = self._fleet(seam=True)
        server = mock.Mock(root=root, state={})
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            self.assertEqual(tokens.availability(root), (True, ""))
            per = {h.name: tokens.today_total(server, h) for h in (tmux, sdk)}
        self.assertEqual(per, {"testa": 15, "wren": 115})   # wren's local transcript not counted
        self.assertEqual(sum(per.values()), 130)

    def test_an_sdk_cousin_counts_without_a_harness_seam(self):
        from cousin_lib.console import tokens
        root, tmux, sdk = self._fleet(seam=False)
        server = mock.Mock(root=root, state={})
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            self.assertEqual(tokens.availability(root), (True, ""))
            self.assertEqual(tokens.today_total(server, sdk), 115)
            self.assertEqual(tokens.today_total(server, tmux), 0)      # no seam: nothing to scan

    def test_a_fleet_with_no_seam_and_no_sdk_cousin_stays_unavailable(self):
        from cousin_lib.console import tokens
        root = _root(self); (root / "config").mkdir(); _cousin(root, "testa", "")
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            self.assertFalse(tokens.availability(root)[0])


if __name__ == "__main__":
    unittest.main()
