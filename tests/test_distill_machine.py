"""Distilled views: the framework's own log does not crowd out authored memory."""
import json
import pathlib
import tempfile
import unittest

from cousin_lib import distill, memory
from tests._hermetic import HermeticCase


def _home(case):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    memory.ensure_layout(home)
    return home


def _raw(home, topic, content, n=1, source="remember"):
    path = memory.raw_dir(home) / "2030-01-01.jsonl"
    with open(path, "a") as fh:
        for i in range(n):
            fh.write(json.dumps({"timestamp": "2030-01-01T10:%02d:00+00:00" % i, "topic": topic,
                                 "content": "%s (%d)" % (content, i), "source": source,
                                 "truth_level": "L3_COUSIN_CONCLUSION"}) + "\n")


def _topics(home, fname="decisions.md"):
    text = (memory.distilled_dir(home) / fname).read_text()
    return [l.rsplit("topic: ", 1)[1].rstrip(")") for l in text.splitlines() if l.startswith("- [")]


class TestMachineTopics(HermeticCase):
    def test_an_authored_topic_keeps_its_line_against_a_busy_machine_topic(self):
        home = _home(self)
        _raw(home, "episode:sess-000", "Checked the ledger totals again.", n=24, source="turn-extract")
        _raw(home, "ledger cadence", "Priya closes the ledger on the first Monday.")
        distill.distill(home, max_lines=1)
        self.assertEqual(_topics(home), ["ledger cadence"])

    def test_each_machine_prefix_yields(self):
        for prefix in ("episode:", "job:", "framework:"):
            with self.subTest(prefix=prefix):
                home = _home(self)
                _raw(home, prefix + "busy", "A machine line.", n=5)
                _raw(home, "the authored one", "Toki keeps the spare keys.")
                distill.distill(home, max_lines=1)
                self.assertEqual(_topics(home), ["the authored one"])

    def test_machine_topics_fill_the_lines_authored_topics_leave(self):
        home = _home(self)
        _raw(home, "job:nightly-build", "The build finished.", n=24)
        _raw(home, "ledger cadence", "Priya closes the ledger on the first Monday.")
        distill.distill(home)
        self.assertEqual(_topics(home), ["ledger cadence", "job:nightly-build"])

    def test_the_generated_header_says_the_log_ranks_last(self):
        home = _home(self)
        _raw(home, "ledger cadence", "Priya closes the ledger on the first Monday.")
        distill.distill(home)
        text = (memory.distilled_dir(home) / "decisions.md").read_text()
        self.assertIn("the framework's own log (episode:, job:, framework:) after every"
                      " authored topic", " ".join(text.split()))

    def test_a_topic_that_only_mentions_a_prefix_is_authored(self):
        home = _home(self)
        _raw(home, "job:nightly-build", "The build finished.", n=3)
        _raw(home, "notes on episode: pilot", "Sam wants the pilot recut.")
        distill.distill(home, max_lines=1)
        self.assertEqual(_topics(home), ["notes on episode: pilot"])


if __name__ == "__main__":
    unittest.main()
