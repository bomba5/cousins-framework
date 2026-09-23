"""SqliteSessionStore: the SDK's adapter suite, plus what it cannot know."""
import asyncio
import pathlib
import tempfile
import unittest

try:
    from claude_agent_sdk.testing import run_session_store_conformance
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.runner.session_store import SqliteSessionStore
from tests._hermetic import HermeticCase

K = {"project_key": "proj", "session_id": "s-1"}


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"; (home / "data").mkdir(parents=True)
    return home


def _assistant(text, uuid):
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class TestConformance(HermeticCase):
    def test_the_sdk_adapter_suite(self):
        asyncio.run(run_session_store_conformance(lambda: SqliteSessionStore(_home(self))))


class TestOwnership(HermeticCase):
    def test_a_second_instance_reads_what_the_first_wrote(self):
        home = _home(self)
        asyncio.run(SqliteSessionStore(home).append(K, [_assistant("a", "u1")]))
        loaded = asyncio.run(SqliteSessionStore(home).load(K))
        self.assertEqual(loaded, [_assistant("a", "u1")])

    def test_a_duplicate_uuid_is_ignored_an_unkeyed_entry_is_not(self):
        home = _home(self); s = SqliteSessionStore(home)
        asyncio.run(s.append(K, [_assistant("a", "u1"), {"type": "title", "title": "t"}]))
        asyncio.run(s.append(K, [_assistant("a", "u1"), {"type": "title", "title": "t"}]))
        types = [e["type"] for e in asyncio.run(s.load(K))]
        self.assertEqual(types, ["assistant", "title", "title"])

    def test_two_sessions_do_not_mix(self):
        home = _home(self); s = SqliteSessionStore(home)
        other = dict(K, session_id="s-2")
        asyncio.run(s.append(K, [_assistant("mine", "u1")]))
        asyncio.run(s.append(other, [_assistant("theirs", "u2")]))
        self.assertEqual(asyncio.run(s.load(K)), [_assistant("mine", "u1")])

    def test_entries_after_is_a_cursor_over_the_main_transcript(self):
        home = _home(self); s = SqliteSessionStore(home)
        asyncio.run(s.append(K, [_assistant("one", "u1"), _assistant("two", "u2")]))
        asyncio.run(s.append(dict(K, subpath="subagents/agent-1"), [_assistant("sub", "u3")]))
        first, cur = s.entries_after("s-1")
        self.assertEqual([e["uuid"] for e in first], ["u1", "u2"])
        asyncio.run(s.append(K, [_assistant("three", "u4")]))
        more, cur2 = s.entries_after("s-1", cur)
        self.assertEqual([e["uuid"] for e in more], ["u4"])
        self.assertEqual(s.entries_after("s-1", cur2), ([], cur2))

    def test_tail_text_reads_a_bounded_tail(self):
        home = _home(self); s = SqliteSessionStore(home)
        asyncio.run(s.append(K, [_assistant("ancient", "u0")] +
                             [_assistant("w%d" % i, "u%d" % (i + 1)) for i in range(300)]))
        self.assertNotIn("ancient", s.tail_text("s-1", max_chars=100000))

    def test_tail_text_is_the_newest_assistant_words(self):
        home = _home(self); s = SqliteSessionStore(home)
        asyncio.run(s.append(K, [_assistant("old words " * 50, "u1"), _assistant("the last word", "u2")]))
        tail = s.tail_text("s-1", max_chars=40)
        self.assertLessEqual(len(tail), 40)
        self.assertTrue(tail.endswith("the last word"))
        self.assertEqual(s.tail_text("nobody"), "")

    def test_the_file_lives_in_the_home(self):
        home = _home(self)
        self.assertEqual(SqliteSessionStore(home).path, home / "data" / "sessions.db")


if __name__ == "__main__":
    unittest.main()
