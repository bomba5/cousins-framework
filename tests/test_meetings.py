"""Meetings: rounds, turns, refusals, skips, closing (docs/meetings.md)."""
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import meetings
from cousin_lib.meetings import MeetingError


class MeetingCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.delivered = []
        self.alive = {}
        self.accept = True
        for slug in ("wren", "toki", "moss"):
            self._cousin(slug)

    def _cousin(self, slug, extra=""):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n[chat]\nport = 8100\n%s'
            % (slug, slug.capitalize(), extra))

    def deliver(self, slug, text):
        if not self.accept:
            return False
        self.delivered.append((slug, text))
        return True

    def is_alive(self, slug):
        return self.alive.get(slug, True)

    def open(self, participants=("wren", "toki"), **kw):
        kw.setdefault("is_alive", self.is_alive)
        return meetings.open_meeting("pick a name", list(participants),
                                     created_by="ana", **kw)

    def post(self, mid, text):
        return meetings.post(mid, "ana", text, deliver=self.deliver)

    def say(self, mid, slug, text):
        return meetings.say(mid, slug, text, deliver=self.deliver)


class TestOpen(MeetingCase):
    def test_refuses_empty_unknown_duplicate_stopped_remote(self):
        with self.assertRaises(MeetingError):
            self.open(())
        with self.assertRaises(MeetingError):
            self.open(("wren", "nobody"))
        with self.assertRaises(MeetingError):
            self.open(("wren", "wren"))
        self.alive["toki"] = False
        with self.assertRaisesRegex(MeetingError, "not running: toki"):
            self.open()
        self._cousin("far", 'host = "far-host"\n')
        with self.assertRaisesRegex(MeetingError, "remote"):
            self.open(("far",))

    def test_opens_on_the_users_floor_with_a_system_line(self):
        m = self.open()
        self.assertEqual((m["state"], m["mode"], m["turn_slug"]),
                         ("open", "floor", ""))
        shown = meetings.show(m["id"])
        self.assertEqual(shown["transcript"][0]["kind"], "system")
        self.assertEqual(self.delivered, [])


class TestRounds(MeetingCase):
    def test_each_participant_once_in_order_then_the_floor(self):
        m = self.open()
        self.post(m["id"], "ideas?")
        self.assertEqual(self.delivered[-1][0], "wren")
        self.assertIn("round 1, your turn", self.delivered[-1][1])
        self.assertIn("ana: ideas?", self.delivered[-1][1])
        self.assertIn("cousin-meeting say %d" % m["id"],
                      self.delivered[-1][1])
        self.say(m["id"], "wren", "Kestrel")
        self.assertEqual(self.delivered[-1][0], "toki")
        # toki sees the question AND wren's answer, in one message
        self.assertIn("ana: ideas?", self.delivered[-1][1])
        self.assertIn("wren: Kestrel", self.delivered[-1][1])
        m = meetings.pass_turn(m["id"], "toki", deliver=self.deliver)
        self.assertEqual((m["mode"], m["turn_slug"]), ("floor", ""))
        self.assertEqual(len(self.delivered), 2)

    def test_second_round_sends_only_what_is_new(self):
        m = self.open()
        self.post(m["id"], "ideas?")
        self.say(m["id"], "wren", "Kestrel")
        self.say(m["id"], "toki", "Heron")
        self.post(m["id"], "vote")
        text = self.delivered[-1][1]
        self.assertIn("round 2", text)
        self.assertIn("toki: Heron", text)   # wren had not seen it
        self.assertNotIn("ideas?", text)     # wren had

    def test_out_of_turn_is_refused_naming_the_speaker(self):
        m = self.open()
        with self.assertRaisesRegex(MeetingError, "floor"):
            self.say(m["id"], "wren", "early")
        self.post(m["id"], "ideas?")
        with self.assertRaisesRegex(MeetingError, "it is wren"):
            self.say(m["id"], "toki", "me first")
        with self.assertRaisesRegex(MeetingError, "wren is speaking"):
            self.post(m["id"], "again")

    def test_direct_question_returns_the_floor_after_one_answer(self):
        m = self.open(("wren", "toki", "moss"))
        self.post(m["id"], "@toki  what did you mean?")
        self.assertEqual(self.delivered[-1][0], "toki")
        self.assertIn("a direct question to you", self.delivered[-1][1])
        m = self.say(m["id"], "toki", "this")
        self.assertEqual(m["mode"], "floor")
        with self.assertRaisesRegex(MeetingError, "not in meeting"):
            self.post(m["id"], "@nobody hi")


class TestTick(MeetingCase):
    def test_undelivered_turn_is_retried(self):
        m = self.open()
        self.accept = False
        self.post(m["id"], "ideas?")
        self.assertFalse(meetings.show(m["id"])["turn_delivered"])
        self.accept = True
        report = meetings.tick(deliver=self.deliver, is_alive=self.is_alive)
        self.assertEqual(self.delivered[-1][0], "wren")
        self.assertIn("turn delivered to wren", report[0])

    def test_silent_speaker_is_skipped_after_the_timeout(self):
        m = self.open(timeout_s=60)
        self.post(m["id"], "ideas?")
        meetings.tick(deliver=self.deliver, is_alive=self.is_alive,
                      now=time.time() + 61)
        shown = meetings.show(m["id"])
        self.assertEqual(shown["turn_slug"], "toki")
        self.assertIn("wren skipped (no answer in 60 s)",
                      shown["transcript"][-1]["text"])

    def test_stopped_speaker_is_skipped(self):
        m = self.open()
        self.post(m["id"], "ideas?")
        self.alive["wren"] = False
        meetings.tick(deliver=self.deliver, is_alive=self.is_alive)
        self.assertEqual(meetings.show(m["id"])["turn_slug"], "toki")


class TestClose(MeetingCase):
    def test_without_facilitator_closes_now(self):
        m = self.open()
        m = meetings.close(m["id"], "ana", deliver=self.deliver)
        self.assertEqual(m["state"], "closed")
        with self.assertRaises(MeetingError):
            self.post(m["id"], "late")

    def test_facilitator_writes_the_minutes_then_it_closes(self):
        m = self.open(facilitator="moss")
        self.post(m["id"], "ideas?")
        m = meetings.close(m["id"], "ana", deliver=self.deliver)
        self.assertEqual((m["state"], m["turn_slug"]), ("closing", "moss"))
        text = self.delivered[-1][1]
        self.assertIn("you facilitate", text)
        self.assertIn("ana: ideas?", text)   # the whole transcript
        with self.assertRaisesRegex(MeetingError, "not your turn"):
            meetings.minutes(m["id"], "wren", "x", deliver=self.deliver)
        with self.assertRaisesRegex(MeetingError, "post the minutes"):
            self.say(m["id"], "moss", "chat")
        m = meetings.minutes(m["id"], "moss", "Decided: Kestrel",
                             deliver=self.deliver)
        self.assertEqual(m["state"], "closed")
        self.assertEqual(meetings.show(m["id"])["transcript"][-1]["kind"],
                         "minutes")

    def test_skip_by_the_user(self):
        m = self.open()
        self.post(m["id"], "ideas?")
        m = meetings.skip(m["id"], "ana", deliver=self.deliver)
        self.assertEqual(m["turn_slug"], "toki")


class TestCli(MeetingCase):
    def test_say_speaks_as_the_cousin_of_cousin_home(self):
        m = self.open()
        with mock.patch.object(meetings, "default_deliver", self.deliver):
            self.post(m["id"], "ideas?")
            with mock.patch.dict(os.environ, {
                    "COUSIN_HOME": str(self.root / "cousins" / "wren")}):
                rc = meetings.meeting_main(["say", str(m["id"]), "Kestrel"])
        self.assertEqual(rc, 0)
        self.assertEqual(meetings.show(m["id"])["turn_slug"], "toki")

    def test_refusal_is_rc_1(self):
        m = self.open()
        with mock.patch.dict(os.environ, {
                "COUSIN_HOME": str(self.root / "cousins" / "toki")}):
            self.assertEqual(
                meetings.meeting_main(["say", str(m["id"]), "x"]), 1)


if __name__ == "__main__":
    unittest.main()


class TestTeach(MeetingCase):
    def test_inserts_above_the_marker_once_and_dry_run_writes_nothing(self):
        home = self.root / "cousins" / "wren"
        (home / "CLAUDE.md").write_text(
            "# Wren\n\n## Voice\nplain\n\n" + meetings.MARKER
            + "\n## Mine\nstuff\n")
        (home / "mcp-registry.toml").write_text("[tools.send]\nkind = 'send'\n")
        before = (home / "CLAUDE.md").read_text()
        changes = meetings.teach()
        self.assertEqual({c[0] for c in changes}, {"wren"})
        self.assertEqual((home / "CLAUDE.md").read_text(), before)
        meetings.teach(apply=True)
        text = (home / "CLAUDE.md").read_text()
        self.assertLess(text.index("## Meetings"), text.index(meetings.MARKER))
        self.assertIn("## Mine\nstuff", text)
        self.assertIn("[tools.meeting]",
                      (home / "mcp-registry.toml").read_text())
        self.assertEqual(meetings.teach(apply=True), [])
