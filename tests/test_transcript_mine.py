"""Flip-time transcript mining.

Only what a cousin deliberately wrote used to survive a flip; the lived
session's conclusions and dead ends evaporated with the harness
transcript. The miner reads the dying session's transcript (one JSON
object per line, assistant turns carrying text blocks) and writes the
sentences that look like conclusions or dead ends as raw candidates,
where the existing raw -> distill pipeline turns them into durable
memory. Best-effort: absent config, absent file, malformed lines all
mean zero candidates and no error.
"""
import json
import tempfile
import unittest
from pathlib import Path

from cousin_lib import transcript_mine


def _line(kind, text=None, *, sidechain=False, tools=0):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for _ in range(tools):
        content.append({"type": "tool_use", "id": "x", "name": "Run",
                        "input": {}})
    return json.dumps({"type": kind, "isSidechain": sidechain,
                       "timestamp": "2026-01-01T10:00:00Z",
                       "message": {"role": kind, "content": content}})


class MineCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "memory").mkdir(parents=True)
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()

    def _configure(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "harness.toml").write_text(
            'transcripts_dir = "%s"\n' % self.transcripts)

    def _write_transcript(self, session_id, lines):
        (self.transcripts / (session_id + ".jsonl")).write_text(
            "\n".join(lines) + "\n")

    def _raw_entries(self):
        raw = self.home / "memory" / "raw"
        if not raw.exists():
            return []
        out = []
        for path in sorted(raw.glob("*.jsonl")):
            out += [json.loads(l) for l in path.read_text().splitlines()
                    if l.strip()]
        return out


class TestMine(MineCase):
    def test_keeps_conclusions_and_dead_ends_skips_noise(self):
        self._configure()
        self._write_transcript("sess-abcdef12-3456", [
            json.dumps({"type": "ai-title", "aiTitle": "x"}),
            _line("user", "operator says the build is red because of me"),
            _line("assistant", "ok."),
            _line("assistant",
                  "I read the config. The cause is the stale cache, so"
                  " clearing it restores the build. The first retry"
                  " failed because the port was busy. Lunch was nice.",
                  tools=2),
            _line("assistant", "Sidechain decided nothing of note here.",
                  sidechain=True),
            "not json at all",
        ])
        n = transcript_mine.mine(self.home, self.root, "sess-abcdef12-3456")
        entries = self._raw_entries()
        contents = [e["content"] for e in entries]
        self.assertEqual(n, len(entries))
        self.assertEqual(n, 2, contents)
        self.assertTrue(any("The cause is the stale cache" in c
                            for c in contents), contents)
        self.assertTrue(any("failed because the port" in c
                            for c in contents), contents)
        # Noise: the operator's own line, the trivial ack, the sidechain,
        # the sentence with no conclusion marker.
        joined = "\n".join(contents)
        self.assertNotIn("operator says", joined)
        self.assertNotIn("Lunch was nice", joined)
        self.assertNotIn("Sidechain", joined)

    def test_entry_shape(self):
        self._configure()
        self._write_transcript("sess-abcdef12-3456", [
            _line("assistant", "We decided to keep the port fixed at"
                  " the configured value.")])
        transcript_mine.mine(self.home, self.root, "sess-abcdef12-3456")
        (entry,) = self._raw_entries()
        self.assertEqual(entry["topic"], "episode:sess-abc")
        self.assertEqual(entry["source"], "flip-transcript")
        self.assertEqual(entry["truth_level"], "L3_COUSIN_CONCLUSION")
        self.assertIn("timestamp", entry)

    def test_cap_respected(self):
        self._configure()
        text = " ".join("Attempt %d failed because the lock was held."
                        % i for i in range(40))
        self._write_transcript("s1", [_line("assistant", text)])
        n = transcript_mine.mine(self.home, self.root, "s1", max_entries=5)
        self.assertEqual(n, 5)
        self.assertEqual(len(self._raw_entries()), 5)

    def test_repeated_sentence_lands_once(self):
        self._configure()
        same = "The cause is the stale cache, so we clear it."
        self._write_transcript("s1", [_line("assistant", same),
                                      _line("assistant", same)])
        self.assertEqual(transcript_mine.mine(self.home, self.root, "s1"), 1)

    def test_missing_transcript_is_zero_without_error(self):
        self._configure()
        self.assertEqual(
            transcript_mine.mine(self.home, self.root, "never-existed"), 0)
        self.assertEqual(self._raw_entries(), [])

    def test_absent_config_is_zero_without_error(self):
        self._write_transcript("s1", [
            _line("assistant", "It failed because the config is absent.")])
        self.assertEqual(transcript_mine.mine(self.home, self.root, "s1"), 0)
        self.assertEqual(self._raw_entries(), [])

    def test_empty_session_id_is_zero(self):
        self._configure()
        self.assertEqual(transcript_mine.mine(self.home, self.root, ""), 0)


class TestHypotheses(MineCase):
    """L4 needs a writer: a kept sentence that hedges is a hypothesis,
    written at L4 under its own topic; the rest stays L3 exactly as
    before."""

    def test_hedged_sentences_go_to_l4_the_rest_stay_l3(self):
        self._configure()
        self._write_transcript("sess-abcdef12-3456", [_line(
            "assistant",
            "The cause is probably the stale cache, so a clear should do."
            " The build failed because the port was busy."
            " I think it failed because the lock file was left behind."
            " Not sure yet, but the retry failed on the same host.")])
        transcript_mine.mine(self.home, self.root, "sess-abcdef12-3456")
        by_level = {}
        for e in self._raw_entries():
            by_level.setdefault(e["truth_level"], []).append(e)
        l3 = by_level["L3_COUSIN_CONCLUSION"]
        l4 = by_level["L4_COUSIN_HYPOTHESIS"]
        self.assertEqual([e["content"] for e in l3],
                         ["The build failed because the port was busy."])
        self.assertEqual(len(l4), 3, l4)
        self.assertEqual({e["topic"] for e in l3}, {"episode:sess-abc"})
        self.assertEqual({e["topic"] for e in l4},
                         {"episode:sess-abc:hypothesis"})

    def test_hedge_words_are_word_bounded(self):
        for sentence in ("The likelihood of a retry failed us.",
                         "A mighty fix: the cause is the lock.",
                         "Maybelline failed because of the dye."):
            self.assertEqual(transcript_mine.level_for(sentence),
                             "L3_COUSIN_CONCLUSION", sentence)
        for sentence in ("It might be the lock, so retry.",
                         "It seems fixed.", "Likely the cache.",
                         "I suspect the port.", "Maybe the disk."):
            self.assertEqual(transcript_mine.level_for(sentence),
                             "L4_COUSIN_HYPOTHESIS", sentence)

    def test_hedging_alone_never_makes_a_sentence_worth_keeping(self):
        self._configure()
        self._write_transcript("s1", [_line(
            "assistant", "I think I will read the configuration next.")])
        self.assertEqual(transcript_mine.mine(self.home, self.root, "s1"), 0)


if __name__ == "__main__":
    unittest.main()
