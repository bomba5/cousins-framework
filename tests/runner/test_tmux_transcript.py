"""The tmux kind's transcript reader: which entries
start a turn, which end one, and how a reader resumes from a byte offset
without ever passing a partial line. The entry shapes are the ones
measured on the interactive CLI 2.1.281; the content is invented."""
import json
import pathlib
import tempfile
import unittest

from cousin_lib.runner import transcript as tr

NONCE = "0123456789ab"


def typed(text, prompt_id="p1", source="typed"):
    return {"type": "user", "promptSource": source, "promptId": prompt_id,
            "entrypoint": "cli", "message": {"role": "user", "content": text}}


def tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": text}]}}


def interrupt(prompt_id="p1", text="[Request interrupted by user]"):
    return {"type": "user", "promptId": prompt_id,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def turn_duration(ms=2441):
    return {"type": "system", "subtype": "turn_duration", "durationMs": ms}


def assistant(text="kestrel-violet", stop_reason="end_turn"):
    return {"type": "assistant", "message": {"role": "assistant", "stop_reason": stop_reason,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 92}}}


def api_error(text):
    return {"type": "assistant", "isApiErrorMessage": True,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def kinds(entries):
    return [e.kind for e in entries]


class TestClassify(unittest.TestCase):
    def c(self, obj):
        return tr.classify(obj, 0, 1)

    def test_a_typed_prompt_is_a_turn_start_with_its_nonce(self):
        e = self.c(typed("[inbox:%s] (Chat Wren) hello\nmore" % NONCE))
        self.assertEqual((e.kind, e.nonce, e.prompt_id, e.prompt_source),
                         ("turn_start", NONCE, "p1", "typed"))

    def test_a_queued_prompt_is_a_turn_start(self):
        e = self.c(typed("[inbox:%s] later" % NONCE, source="queued"))
        self.assertEqual((e.kind, e.prompt_source), ("turn_start", "queued"))

    def test_the_nonce_counts_only_at_the_start_of_the_first_line(self):
        self.assertIsNone(self.c(typed("hello [inbox:%s]" % NONCE)).nonce)
        self.assertIsNone(self.c(typed("hello\n[inbox:%s] x" % NONCE)).nonce)

    def test_a_pasted_body_does_not_hide_the_typed_nonce(self):
        text = "[inbox:%s] (Chat Wren) read the paste\n\n<pasted_content id=\"d22a\">\nbody\n</pasted_content>" % NONCE
        self.assertEqual(self.c(typed(text)).nonce, NONCE)

    def test_a_tool_result_is_never_a_turn_start(self):
        e = self.c(tool_result("[inbox:%s] echoed by a tool" % NONCE))
        self.assertEqual((e.kind, e.nonce), ("tool_result", None))

    def test_meta_and_sourceless_user_entries_are_other(self):
        meta = typed("<local-command-stdout>ok</local-command-stdout>")
        meta["isMeta"] = True
        self.assertEqual(self.c(meta).kind, "other")
        sdk_era = {"type": "user", "entrypoint": "sdk-py", "promptSource": "sdk",
                   "message": {"role": "user", "content": "[inbox:%s] from the SDK lane" % NONCE}}
        self.assertEqual(self.c(sdk_era).kind, "other")

    def test_both_interrupt_texts(self):
        self.assertEqual(self.c(interrupt()).kind, "interrupt")
        self.assertEqual(self.c(interrupt(text="[Request interrupted by user for tool use]")).kind,
                         "interrupt")
        self.assertEqual(self.c(interrupt()).prompt_id, "p1")

    def test_an_empty_typed_prompt_is_not_a_turn_start(self):
        self.assertEqual(self.c(typed("  ")).kind, "other")

    def test_a_typed_prompt_opening_with_the_interrupt_text_is_a_prompt(self):
        e = self.c(typed("[Request interrupted by user] was what I saw"))
        self.assertEqual(e.kind, "turn_start")

    def test_turn_duration_ends_a_turn(self):
        self.assertEqual(self.c(turn_duration()).kind, "turn_end")

    def test_api_errors_and_the_limit_wording(self):
        self.assertEqual(self.c(api_error("API Error: 500 overloaded")).kind, "api_error")
        for text in ("Claude usage limit reached. Your limit resets at 5pm",
                     "5-hour limit reached", "You've hit your usage limit"):
            self.assertEqual(self.c(api_error(text)).kind, "limit", text)

    def test_an_ordinary_assistant_entry(self):
        self.assertEqual(self.c(assistant()).kind, "assistant")


class TestReadFrom(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "s.jsonl"

    def write(self, objs, tail=""):
        text = "".join(json.dumps(o) + "\n" for o in objs) + tail
        self.path.write_text(text)
        return text

    def test_offsets_resume_exactly(self):
        self.write([typed("[inbox:%s] a" % NONCE), assistant(), turn_duration()])
        entries, end = tr.read_from(self.path, 0)
        self.assertEqual(kinds(entries), ["turn_start", "assistant", "turn_end"])
        self.assertEqual(end, self.path.stat().st_size)
        self.assertEqual(entries[1].offset, entries[0].end)
        again, end2 = tr.read_from(self.path, entries[1].offset)
        self.assertEqual(kinds(again), ["assistant", "turn_end"])
        self.assertEqual(end2, end)

    def test_never_past_a_partial_last_line(self):
        self.write([typed("[inbox:%s] a" % NONCE)], tail='{"type": "assist')
        entries, end = tr.read_from(self.path, 0)
        self.assertEqual(kinds(entries), ["turn_start"])
        self.assertEqual(end, entries[0].end)
        with self.path.open("a") as fh:
            fh.write('ant"}\n')
        more, _ = tr.read_from(self.path, end)
        self.assertEqual(len(more), 1)

    def test_an_unparsable_complete_line_is_other_and_kept_raw(self):
        self.path.write_text("not json [inbox:%s]\n" % NONCE + json.dumps(turn_duration()) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual(kinds(entries), ["other", "turn_end"])
        self.assertIn(NONCE, entries[0].raw["_unparsed"])

    def test_a_missing_file_reads_nothing(self):
        self.assertEqual(tr.read_from(self.path, 0), ([], 0))

    def test_an_sdk_era_stretch_starts_no_turn(self):
        sdk_user = {"type": "user", "entrypoint": "sdk-py", "promptSource": "sdk",
                    "message": {"role": "user", "content": "[inbox:%s] sdk" % NONCE}}
        self.write([sdk_user, assistant(), typed("[inbox:%s] tmux" % NONCE, prompt_id="p9")])
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual(kinds(entries), ["other", "assistant", "turn_start"])


class TestTornLines(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "s.jsonl"

    def test_a_torn_line_merged_with_a_turn_start_still_names_its_known_nonce(self):
        # SIGKILL tore a line; the next CLI appended its entry to the fragment
        self.path.write_text('{"type": "assistant", "mess')
        entries, cursor = tr.read_from(self.path, 0)
        self.assertEqual((entries, cursor), ([], 0))
        with self.path.open("a") as fh:
            fh.write(json.dumps(typed("[inbox:%s] hello" % NONCE)) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual(kinds(entries), ["other", "turn_start"])
        self.assertEqual(tr.turn_nonce(entries[1], {NONCE}), NONCE)
        self.assertIsNone(tr.turn_nonce(entries[1], {"0" * 12}), "only a known nonce counts")

    def test_a_torn_head_before_a_complete_turn_start_is_recovered_and_classified(self):
        self.path.write_text('{"type": "assistant", "mess' + json.dumps(typed("[inbox:%s] hi" % NONCE)) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual([(e.kind, e.nonce) for e in entries], [("other", None), ("turn_start", NONCE)])
        self.assertTrue(entries[0].raw["fragments_dropped"])

    def test_a_torn_head_before_a_tool_result_opening_with_the_nonce_is_no_turn_start(self):
        # a tmux cousin reading this repo's fixtures gets a tool result that opens with a nonce
        self.path.write_text('{"type": "assistant", "mess'
                             + json.dumps(tool_result("[inbox:%s] from a fixture" % NONCE)) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual([e.kind for e in entries], ["other", "tool_result"])
        self.assertEqual([tr.turn_nonce(e, {NONCE}) for e in entries], [None, None])

    def test_two_tears_in_a_row_keep_every_complete_entry(self):
        middle = typed("[inbox:%s] the middle" % NONCE, prompt_id="p2")
        self.path.write_text('{"type": "assistant", "mess' + json.dumps(middle)
                             + json.dumps(turn_duration()) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual([(e.kind, e.nonce) for e in entries],
                         [("other", None), ("turn_start", NONCE), ("turn_end", None)])
        self.assertTrue(entries[0].raw["fragments_dropped"])

    def test_a_torn_middle_is_kept_as_fragments_and_its_nonce_still_found(self):
        middle = json.dumps(typed("[inbox:%s] torn too" % NONCE, prompt_id="p2"))[:-7]
        self.path.write_text('{"type": "assistant", "mess' + middle + json.dumps(turn_duration()) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual([e.kind for e in entries], ["other", "turn_end"])
        self.assertTrue(entries[0].raw["fragments_dropped"])
        self.assertEqual(tr.turn_nonce(entries[0], {NONCE}), NONCE)

    def test_a_content_block_ending_a_torn_head_is_no_entry(self):
        head = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}'
        self.path.write_text(head + json.dumps(turn_duration()) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual([e.kind for e in entries], ["other", "turn_end"])

    def test_the_raw_fallback_needs_a_user_entry_without_a_tool_result(self):
        # neither tail parses (the complete entry is itself cut): only the raw search is left
        head = '{"type": "assistant", "mess'
        user = json.dumps(typed("[inbox:%s] hi" % NONCE))[:-1]
        tool = json.dumps(tool_result("[inbox:%s] out" % NONCE))[:-1]
        for body, want in ((user, NONCE), (tool, None)):
            e = tr.Entry(0, 1, "other", raw={"_unparsed": head + body})
            self.assertEqual(tr.turn_nonce(e, {NONCE}), want, body[:40])

    def test_a_nonce_inside_a_string_is_not_found(self):
        self.path.write_text('{"type": "ass' + json.dumps(tool_result("seen [inbox:%s] mid" % NONCE)) + "\n")
        entries, _ = tr.read_from(self.path, 0)
        self.assertEqual(tr.raw_nonces(entries[0], {NONCE}), [])


class TestLocate(unittest.TestCase):
    def test_the_config_dir_and_the_encoded_home(self):
        p = tr.locate("/srv/u/cf/cousins/wren", session_id="sid-1",
                      config_dir=pathlib.Path("/cfg"))
        self.assertEqual(p, pathlib.Path("/cfg/projects/-srv-u-cf-cousins-wren/sid-1.jsonl"))

    def test_no_config_dir_is_the_host_login(self):
        p = tr.locate("/h/w", session_id="s", config_dir=None)
        self.assertEqual(p, pathlib.Path("~/.claude/projects/-h-w/s.jsonl").expanduser())


class TestTranscriptStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = pathlib.Path(self.dir.name) / "s.jsonl"

    def say(self, text):
        return json.dumps({"type": "assistant", "message": {"role": "assistant",
                           "content": [{"type": "text", "text": text}]}}) + "\n"

    def test_entries_after_is_a_byte_cursor_that_never_passes_a_torn_line(self):
        whole = self.say("one")
        self.path.write_text(whole + self.say("two")[:10])
        store = tr.TranscriptStore(self.path, "sid")
        entries, cursor = store.entries_after("sid", 0)
        self.assertEqual((len(entries), cursor), (1, len(whole)))
        self.assertEqual(store.entries_after("sid", cursor), ([], cursor))
        self.assertEqual(store.entries_after("other", 0), ([], 0), "another session's cursor is not this file's")

    def test_tail_text_is_the_assistant_text_bounded(self):
        self.path.write_text(self.say("early") + self.say("late"))
        store = tr.TranscriptStore(self.path, "sid")
        self.assertEqual(store.tail_text("sid"), "early\nlate")
        self.assertEqual(store.tail_text("sid", max_chars=4), "late")


if __name__ == "__main__":
    unittest.main()


class TestLastLineEnd(unittest.TestCase):
    """Where a runner that starts now begins reading: just past the last
    complete line, so a line still being written is never a fragment."""

    def test_the_offset_is_past_the_last_newline(self):
        import tempfile
        from cousin_lib.runner import transcript
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "t.jsonl"
            self.assertEqual(transcript.last_line_end(path), 0)              # missing
            path.write_bytes(b"")
            self.assertEqual(transcript.last_line_end(path), 0)
            path.write_bytes(b'{"a": 1}\n{"b": 2}\n')
            self.assertEqual(transcript.last_line_end(path), path.stat().st_size)
            path.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c": half')
            self.assertEqual(transcript.last_line_end(path), len(b'{"a": 1}\n{"b": 2}\n'))
            path.write_bytes(b'no newline at all')
            self.assertEqual(transcript.last_line_end(path), 0)
            big = b"x" * 70000 + b"\n" + b"y" * 70000                    # across chunks
            path.write_bytes(big)
            self.assertEqual(transcript.last_line_end(path, chunk=4096), 70001)
            entries, _ = transcript.read_from(path, transcript.last_line_end(path))
            self.assertEqual(entries, [])                                  # no fragment

