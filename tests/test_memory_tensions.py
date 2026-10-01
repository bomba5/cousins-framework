"""The tensions view: topics whose live claims
disagree, for the operator to settle. "Disagree" is not judged by a model
here: an authored topic with two or more live
claims of different content is a tension, the way a later correction or a
"RESOLVED" beside the claim it resolves is; settling one claim (obsolete
--entry) clears it. Machine topics (the framework's own log) never are."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory
from tests._hermetic import HermeticCase
from tests.console._harness import ConsoleCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
    p.start(); case.addCleanup(p.stop)
    return home


def _seed(home):
    rdir = memory.raw_dir(home); rdir.mkdir(parents=True, exist_ok=True)
    rows = [
        ("2026-01-01T10:00:00+00:00", "keys", "Toki keeps the spare keys in the blue tin."),
        ("2026-01-02T10:00:00+00:00", "keys", "RESOLVED: the spare keys moved to the shed."),
        ("2026-01-01T10:00:00+00:00", "ferns", "Sam waters the ferns."),
        ("2026-01-01T10:00:00+00:00", "framework:flip", "flipped to generation 2"),
        ("2026-01-02T10:00:00+00:00", "framework:flip", "flipped to generation 3"),
        ("2026-01-01T10:00:00+00:00", "echo", "same words"),
        ("2026-01-02T10:00:00+00:00", "echo", "same  words"),
    ]
    with open(rdir / "2026-01-01.jsonl", "a") as fh:
        for ts, topic, content in rows:
            fh.write(json.dumps({"timestamp": ts, "topic": topic, "content": content,
                                 "truth_level": "L3_COUSIN_CONCLUSION"}) + "\n")


class TestTensions(HermeticCase):
    def test_two_live_claims_that_differ_are_a_tension(self):
        home = _home(self)
        _seed(home)
        [t] = memory.tensions(home)
        self.assertEqual(t["topic"], "keys")
        self.assertEqual([c["content"][:8] for c in t["claims"]], ["Toki kee", "RESOLVED"])
        self.assertTrue(all(len(c["id"]) == 12 for c in t["claims"]))

    def test_settling_one_claim_clears_it(self):
        home = _home(self)
        _seed(home)
        stale = memory.tensions(home)[0]["claims"][0]["id"]
        memory.mark_obsolete(home, "keys", "the tin is gone", entry=stale)
        self.assertEqual(memory.tensions(home), [])

    def test_the_log_and_repeated_words_are_never_tensions(self):
        home = _home(self)
        _seed(home)
        self.assertNotIn("framework:flip", [t["topic"] for t in memory.tensions(home)])
        self.assertNotIn("echo", [t["topic"] for t in memory.tensions(home)])


class TestCli(HermeticCase):
    def _main(self, home, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memory.memory_main(["--home", str(home), *argv])
        return rc, out.getvalue()

    def test_text_and_json(self):
        home = _home(self)
        _seed(home)
        rc, out = self._main(home, "tensions")
        self.assertEqual(rc, 0)
        self.assertIn("keys (2 live claims)", out)
        self.assertIn("obsolete keys --why", out)            # says how to settle
        rc, out = self._main(home, "tensions", "--json")
        self.assertEqual([t["topic"] for t in json.loads(out)], ["keys"])

    def test_the_settle_hint_quotes_a_topic_with_spaces(self):
        """The printed command must run as printed."""
        home = _home(self)
        memory.remember(home, "spare keys", "The spare keys are in the blue tin.")
        memory.remember(home, "spare keys", "The spare keys moved to the shed.")
        rc, out = self._main(home, "tensions")
        self.assertIn("cousin-memory obsolete 'spare keys' --why", out)

    def test_none_says_so(self):
        home = _home(self)
        rc, out = self._main(home, "tensions")
        self.assertEqual((rc, out.strip()), (0, "no tensions"))


class TestTheRunnersTool(HermeticCase):
    def test_the_memory_tool_settles_one_by_its_id(self):
        """A runner cousin settles a tension through its own
        memory tool, as the CLI and the console can."""
        import types
        from cousin_lib.runner import tools
        home = _home(self)
        _seed(home)
        stale = memory.tensions(home)[0]["claims"][0]["id"]
        ctx = types.SimpleNamespace(home=home, slug="wren")
        tools._m_obsolete(ctx, {"topic": "keys", "why": "the tin is gone", "entry": stale})
        self.assertEqual(memory.tensions(home), [])
        self.assertIn("keys", [r["topic"] for r in memory.live_entries(home)])


class TestRoute(ConsoleCase):
    def test_the_console_lists_a_cousins_tensions(self):
        home = self.cousin("wren")
        _seed(home)
        self.serve()
        status, body = self.get("/api/memory/wren/tensions")
        self.assertEqual(status, 200, body)
        self.assertEqual([t["topic"] for t in body["tensions"]], ["keys"])

    def test_the_console_settles_one_by_its_id(self):
        """What the console lists, it can settle."""
        home = self.cousin("wren")
        _seed(home)
        self.serve()
        stale = memory.tensions(home)[0]["claims"][0]["id"]
        status, body = self.post("/api/memory/wren/obsolete",
                                 {"topic": "keys", "why": "the tin is gone", "entry": stale})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entry"]["entry"], stale)
        status, body = self.get("/api/memory/wren/tensions")
        self.assertEqual(body["tensions"], [])
        status, body = self.post("/api/memory/wren/obsolete",
                                 {"topic": "keys", "why": "x", "entry": "000000000000"})
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
