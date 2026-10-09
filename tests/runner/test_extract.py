"""Continuous extraction: idempotent, deduplicated, capped per rolling day."""
import asyncio
import json
import pathlib
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    import claude_agent_sdk  # noqa: F401 - the store's append folds a summary with it
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib import memory, transcript_mine
from cousin_lib.runner import extract
from cousin_lib.runner.session_store import SqliteSessionStore
from tests._hermetic import HermeticCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True); (home / "memory").mkdir()
    return home


def _turn(store, sid, n, sentences):
    entries = [{"type": "user", "uuid": "%s-u%d" % (sid, n),
                "message": {"role": "user", "content": "[operator:priya] chat from Priya\n\ngo"}},
               {"type": "assistant", "uuid": "%s-a%d" % (sid, n), "message": {"role": "assistant",
                "content": [{"type": "text", "text": " ".join(sentences)}]}}]
    asyncio.run(store.append({"project_key": "p", "session_id": sid}, entries))


def _raw(home):
    out = []
    for path in sorted((home / "memory" / "raw").glob("*.jsonl")):
        out += [json.loads(l) for l in path.read_text().splitlines()]
    return out


class TestMineTurn(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = _home(self); self.store = SqliteSessionStore(self.home)

    def test_a_turn_is_mined_with_the_flip_miners_rules(self):
        _turn(self.store, "sess-0001", 1, ["The cache broke because the cwd moved between runs.",
                                             "ok"])
        self.assertEqual(extract.mine_turn(self.home, "sess-0001", 1, store=self.store), 1)
        entry = _raw(self.home)[0]
        self.assertEqual(entry["topic"], "episode:sess-000")
        self.assertEqual(entry["source"], extract.SOURCE)
        self.assertEqual(entry["truth_level"], transcript_mine.TRUTH_LEVEL)

    def test_idempotent_on_re_run(self):
        _turn(self.store, "sess-0001", 1, ["Decided to keep the store because resume reads it."])
        extract.mine_turn(self.home, "sess-0001", 1, store=self.store)
        self.assertEqual(extract.mine_turn(self.home, "sess-0001", 1, store=self.store), 0)
        (self.home / "data" / "extract-cursor.json").unlink()      # even with the cursor lost
        self.assertEqual(extract.mine_turn(self.home, "sess-0001", 1, store=self.store), 0)
        self.assertEqual(len(_raw(self.home)), 1)

    def test_dedupe_spans_the_flip_miner(self):
        memory._append_raw(self.home, {"topic": "episode:old", "source": transcript_mine.SOURCE,
                                       "content": "The build failed because the overlay was missing.",
                                       "truth_level": transcript_mine.TRUTH_LEVEL})
        _turn(self.store, "sess-0002", 1, ["the build  FAILED because the overlay was missing."])
        self.assertEqual(extract.mine_turn(self.home, "sess-0002", 1, store=self.store), 0)

    def test_the_cap_holds_across_300_turns(self):
        for n in range(1, 301):
            _turn(self.store, "sess-0003", n,
                  ["Turn %d decided the order because step %d was ready." % (n, n),
                   "Turn %d failed the probe because pin %d floated." % (n, n)])
            extract.mine_turn(self.home, "sess-0003", n, store=self.store)
        self.assertEqual(len(_raw(self.home)), extract.WINDOW_CAP)

    def test_a_rollover_does_not_open_a_new_window(self):
        for sid in ("sess-000a", "sess-000b"):          # two generations, one day
            for n in range(1, 40):
                _turn(self.store, sid, n, ["%s turn %d fixed the pin because it floated." % (sid, n)])
                extract.mine_turn(self.home, sid, n, store=self.store)
        self.assertEqual(len(_raw(self.home)), extract.WINDOW_CAP)

    def test_the_window_rolls(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        raw = self.home / "memory" / "raw"; raw.mkdir(exist_ok=True)
        with open(raw / ("%s.jsonl" % date.today().isoformat()), "a") as fh:   # today's file, old stamps
            for i in range(extract.WINDOW_CAP):
                fh.write(json.dumps({"timestamp": old, "topic": "episode:x", "source": extract.SOURCE,
                                     "content": "Old sentence %d because of age." % i}) + "\n")
        self.assertEqual(extract.written_in_window(self.home), 0)
        _turn(self.store, "sess-0004", 1, ["Fresh and kept because the window rolled."])
        self.assertEqual(extract.mine_turn(self.home, "sess-0004", 1, store=self.store), 1)

    def test_an_error_is_minus_one_not_an_exception(self):
        class Broken:
            def entries_after(self, *a, **k):
                raise OSError("disk gone")
        self.assertEqual(extract.mine_turn(self.home, "s", 1, store=Broken()), -1)


class TestRunnerWiring(HermeticCase):
    """The runner mines after each result, off the loop, and a failing
    extraction is an `extract` event, never a failed turn."""

    def _run(self, store, attach=True):
        from cousin_lib.delivery import Item
        from cousin_lib.runner.sdk import SdkRunner
        from tests.runner.test_sdk import ScriptedClient, _wait, assistant, init_msg, result
        home = self.home
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(
            o, [[init_msg(session="sess-0009"), assistant(text="ok"), result(session="sess-0009")]]))
        if attach:
            r.session_store = store
        else:
            r.session_store = None   # a construction path that set none (options() still builds)
        self.addCleanup(lambda: r.stop(timeout=5))
        r.start()
        rec = r.enqueue(Item("operator:priya", "chat", "hi", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "extract" for e in r.events())))
        self.assertEqual(r.inbox.get(rec.inbox_id)["outcome"], "delivered")
        return [e["payload"] for e in r.events() if e["kind"] == "extract"]

    def setUp(self):
        super().setUp()
        self.home = _home(self)

    def test_a_result_mines_the_sessions_new_entries(self):
        store = SqliteSessionStore(self.home)
        _turn(store, "sess-0009", 1, ["The pin floated because the pull-up was missing."])
        events = self._run(store)
        self.assertEqual(events, [{"session_id": "sess-0009", "turn": 1, "written": 1}])
        self.assertEqual([e["turn"] for e in _raw(self.home)], [1])

    def test_a_failing_extraction_is_an_event_not_a_failed_turn(self):
        class Broken:
            def entries_after(self, *a, **k):
                raise OSError("disk gone")
        self.assertEqual(self._run(Broken())[0]["written"], -1)

    def test_a_runner_without_a_store_says_it_skipped(self):
        events = self._run(None, attach=False)
        self.assertEqual(events, [{"session_id": "sess-0009", "turn": 1, "written": 0,
                                   "skipped": "no session store"}])


class TestTranscriptMineSplit(HermeticCase):
    def test_texts_from_entries_skips_sidechains_and_non_text(self):
        entries = [{"type": "assistant", "isSidechain": True,
                    "message": {"content": [{"type": "text", "text": "side"}]}},
                   {"type": "assistant", "message": {"content": [{"type": "tool_use"},
                                                                 {"type": "text", "text": "main"}]}}]
        self.assertEqual(list(transcript_mine.texts_from_entries(entries)), ["main"])

    def test_normalize(self):
        self.assertEqual(transcript_mine.normalize("  The  Cause\nwas X. "), "the cause was x.")


if __name__ == "__main__":
    unittest.main()


class TestIdleTurns(HermeticCase):
    """Meeting 11 B: a turn nobody in the chat started, that changed
    nothing, is not mined and not proposed about."""

    def setUp(self):
        super().setUp()
        self.home = _home(self); self.store = SqliteSessionStore(self.home)

    def entries(self, opener, tools=()):
        content = [{"type": "tool_use", "id": "t%d" % i, "name": name, "input": inp}
                   for i, (name, inp) in enumerate(tools)]
        content.append({"type": "text",
                        "text": "Decided nothing changed because the inbox was empty."})
        return [{"type": "user", "message": {"role": "user", "content": opener}},
                {"type": "assistant", "message": {"role": "assistant", "content": content}}]

    def test_what_counts_as_idle(self):
        beat = "[loop:daemon] loop from unknown at 10:00\n\nContext heartbeat."
        cases = [
            (beat, (), True),
            (beat, [("Bash", {"command": "ls"}), ("mcp__cousin__memory", {"command": "activity"})], True),
            (beat, [("mcp__cousin__reply", {"text": "hi"})], False),
            (beat, [("mcp__cousin__send", {"to": "kestrel"})], False),
            (beat, [("mcp__cousin__memory", {"command": "decide"})], False),
            (beat, [("Edit", {})], False),
            ("[system] job from framework at 10:00\n\njob #3 done", (), True),
            ("[operator:priya] chat from Priya at 10:00\n\nhow is it going", (), False),
            ("[meeting:11] meeting from unknown", (), False),
            ("[peer:kestrel] chat from Kestrel", (), False),
        ]
        for opener, tools, idle in cases:
            with self.subTest(opener=opener[:20], tools=tools):
                self.assertEqual(extract.idle_turn(self.entries(opener, tools)), idle)

    def test_an_idle_heartbeat_is_neither_mined_nor_proposed(self):
        asyncio.run(self.store.append({"project_key": "p", "session_id": "sess-9"},
                                      self.entries("[loop:daemon] loop from unknown\n\nbeat")))
        self.assertEqual(extract.mine_turn(self.home, "sess-9", 1, store=self.store), 0)
        self.assertIsNone(extract.propose_turn(self.home, "sess-9", store=self.store))
        self.assertEqual(_raw(self.home), [])
