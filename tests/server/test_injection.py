"""Terminal delivery: line composition and the tmux wire sequence.

Composition is pure and tested directly. The wire sequence is tested
against a fake tmux executable that records its argv and serves a
scripted pane capture - the injector shells out to whatever binary it is
given, so the fake exercises the real subprocess path.
"""
import io
import os
import pathlib
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from cousin_lib.server.injection import (
    SEND_KEYS_MAX_BYTES,
    TmuxInjector,
    compose_delivery,
    default_settle,
    make_deliver,
)
from tests._fakes import _FAKE_TMUX


class ComposeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.marker = self.home / ".last-user-msg"
        self.now = datetime(2026, 8, 6, 5, 30, tzinfo=timezone.utc)

    def test_prefix_reports_gap_since_previous_message(self):
        self.marker.touch()
        twelve_minutes_ago = self.now.timestamp() - 12 * 60
        os.utime(self.marker, (twelve_minutes_ago, twelve_minutes_ago))
        line = compose_delivery("Sam", "hello", marker_path=self.marker,
                                now=self.now)
        self.assertEqual(
            line,
            "[now: 2026-08-06 05:30 UTC | dt-since-msg: 12m]"
            " (Chat Sam): hello",
        )

    def test_prefix_degrades_to_now_alone_without_marker(self):
        line = compose_delivery("Sam", "hello", marker_path=self.marker,
                                now=self.now)
        self.assertEqual(
            line, "[now: 2026-08-06 05:30 UTC] (Chat Sam): hello"
        )

    def test_single_line_by_construction(self):
        # An embedded newline would split the paste into a premature
        # submit; composition flattens, the wire never sees one.
        line = compose_delivery("Sam", "one\ntwo\r\nthree",
                                marker_path=self.marker, now=self.now)
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertIn("one two three", line)

    def test_attachment_markers_are_appended(self):
        line = compose_delivery(
            "Sam", "look", marker_path=self.marker, now=self.now,
            attachments=["[image attached -> Read /somewhere/1.png]"],
        )
        self.assertTrue(
            line.endswith("look [image attached -> Read /somewhere/1.png]")
        )

    def test_suffix_provider_output_is_appended(self):
        line = compose_delivery(
            "Sam", "hello", marker_path=self.marker, now=self.now,
            suffix_provider=lambda: "[recall] earlier thread",
        )
        self.assertTrue(line.endswith("hello [recall] earlier thread"))

    def test_failing_suffix_provider_never_loses_the_delivery(self):
        def boom():
            raise RuntimeError("recall backend down")

        line = compose_delivery("Sam", "hello", marker_path=self.marker,
                                now=self.now, suffix_provider=boom)
        self.assertTrue(line.endswith("(Chat Sam): hello"))


class InjectorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.tmux = root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.log = root / "calls.log"
        self.pane = root / "pane.txt"
        patcher = mock.patch.dict(os.environ, {
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(self.pane),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.errors = io.StringIO()

    def _injector(self):
        return TmuxInjector("wren", tmux_bin=str(self.tmux),
                            settle=lambda n: 0, verify_delay=0,
                            log=self.errors)

    def _calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class TestInjector(InjectorCase):
    def test_wire_sequence_is_paste_then_enter_then_verify(self):
        self.pane.write_text("> _\n")  # empty input box: submitted
        self._injector().inject("hello there")
        calls = self._calls()
        self.assertEqual(calls, [
            "send-keys -t wren -l hello there",
            "send-keys -t wren Enter",
            "capture-pane -p -t wren",
        ])

    def test_retries_enter_exactly_once_when_text_never_leaves_the_box(self):
        # The pane is static in this fake, so the text "stays" in the
        # input box even after the retry - which is exactly the case that
        # must not loop.
        self.pane.write_text("> hello there\n")
        self._injector().inject("hello there")
        enters = [c for c in self._calls() if c.endswith("Enter")]
        self.assertEqual(len(enters), 2)

    def test_echo_above_an_empty_input_box_reads_as_submitted(self):
        # The submitted message often re-renders directly above the input
        # box. That echo must not read as "still in the box": the retry
        # exists to rescue a stranded message, and firing it here fires
        # it exactly when nothing was stranded.
        self.pane.write_text(
            "> hello there\n"
            "+----------+\n"
            "| >        |\n"
            "+----------+\n"
        )
        self._injector().inject("hello there")
        enters = [c for c in self._calls() if c.endswith("Enter")]
        self.assertEqual(len(enters), 1)

    def test_failed_paste_stops_the_sequence(self):
        # A dead or renamed session fails the paste; pressing Enter into
        # it anyway is two pointless tmux calls and a misleading second
        # failure line.
        with mock.patch.dict(os.environ, {"FAKE_TMUX_RC": "1"}):
            self._injector().inject("hello")
        self.assertEqual(len(self._calls()), 1)

    def test_failed_paste_is_logged_loudly_not_raised(self):
        with mock.patch.dict(os.environ, {"FAKE_TMUX_RC": "1"}):
            self._injector().inject("hello")
        self.assertIn("FAILED", self.errors.getvalue())

    def test_concurrent_injects_do_not_interleave(self):
        self.pane.write_text("")
        with mock.patch.dict(os.environ, {"FAKE_TMUX_PASTE_DELAY": "0.05"}):
            injector = self._injector()
            threads = [
                threading.Thread(target=injector.inject, args=("m%d" % i,))
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        calls = self._calls()
        # Serialized: each paste is followed by its own Enter before the
        # next paste starts.
        paste_indexes = [i for i, c in enumerate(calls) if " -l " in c]
        for idx in paste_indexes:
            self.assertTrue(calls[idx + 1].endswith("Enter"),
                            "interleaved: %r" % calls)

    def test_inject_async_returns_before_delivery_and_still_delivers(self):
        self.pane.write_text("")
        thread = self._injector().inject_async("hello")
        thread.join(timeout=5)
        self.assertTrue(any(" -l " in c for c in self._calls()))


class TestLongLine(InjectorCase):
    """tmux refuses a command over its ~16 KB message size, and
    send-keys carries the text as an argument: a long line goes through
    a paste buffer instead (seen live 2026-09-18, a loops delivery
    failing every tick with "command too long")."""

    def setUp(self):
        super().setUp()
        self.stdin = self.log.parent / "stdin.txt"
        patcher = mock.patch.dict(os.environ,
                                  {"FAKE_TMUX_STDIN": str(self.stdin)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_short_line_stays_on_send_keys(self):
        self.pane.write_text("> _\n")
        self._injector().inject("x" * SEND_KEYS_MAX_BYTES)
        self.assertTrue(self._calls()[0].startswith("send-keys -t wren -l "))
        self.assertFalse(self.stdin.exists())

    def test_long_line_goes_through_a_paste_buffer(self):
        self.pane.write_text("> _\n")
        text = "y" * (SEND_KEYS_MAX_BYTES + 1)
        self.assertTrue(self._injector().inject(text))
        self.assertEqual(self._calls(), [
            "load-buffer -b cf-inject-wren -",
            "paste-buffer -b cf-inject-wren -d -t wren",
            "send-keys -t wren Enter",
            "capture-pane -p -t wren",
        ])
        self.assertEqual(self.stdin.read_text(), text)

    def test_multibyte_length_counts_bytes_not_characters(self):
        self.pane.write_text("> _\n")
        self._injector().inject("\u00e8" * (SEND_KEYS_MAX_BYTES // 2 + 1))
        self.assertTrue(self._calls()[0].startswith("load-buffer"))

    def test_failed_buffer_load_stops_before_any_keys(self):
        with mock.patch.dict(os.environ, {"FAKE_TMUX_RC": "1"}):
            ok = self._injector().inject("z" * (SEND_KEYS_MAX_BYTES + 1))
        self.assertFalse(ok)
        self.assertEqual(self._calls(), ["load-buffer -b cf-inject-wren -"])
        self.assertIn("FAILED", self.errors.getvalue())


class TestMakeDeliver(InjectorCase):
    def test_composes_and_injects_the_chat_line(self):
        home = self.tmux.parent / "home"
        home.mkdir()
        deliver = make_deliver(home, self._injector())
        self.pane.write_text("")
        deliver(user="Sam", message="hello", message_id=1,
                attachments=["[image attached -> Read /x/1.png]"])
        for _ in range(100):
            if self._calls():
                break
            time.sleep(0.02)
        paste = next(c for c in self._calls() if " -l " in c)
        self.assertIn("(Chat Sam): hello [image attached", paste)
        self.assertIn("[now: ", paste)


class TestAttentionGuard(InjectorCase):
    """A pane parked on a login or trust menu is waiting on a person:
    typed text there selects menu options (the install re-test saw a
    fresh cousin's pane move from the theme picker to the login menu
    before anyone had written to it). Every injection skips it."""

    MENU = "Select login method:\n 1. Claude account\n 2. API key\n"

    def _guarded(self, patterns=("Select login method",)):
        return TmuxInjector("wren", tmux_bin=str(self.tmux),
                            settle=lambda n: 0, verify_delay=0,
                            log=self.errors,
                            attention_patterns=list(patterns))

    def test_menu_pane_is_never_typed_into(self):
        self.pane.write_text(self.MENU)
        typed = self._guarded().inject("Context heartbeat.\nline two")
        self.assertFalse(typed)
        self.assertEqual(self._calls(), ["capture-pane -p -t wren"])
        self.assertIn("SKIPPED", self.errors.getvalue())
        self.assertIn("Select login method", self.errors.getvalue())

    def test_ready_pane_is_typed_into_after_the_check(self):
        self.pane.write_text("> _\n")
        typed = self._guarded().inject("hello there")
        self.assertTrue(typed)
        self.assertEqual(self._calls(), [
            "capture-pane -p -t wren",
            "send-keys -t wren -l hello there",
            "send-keys -t wren Enter",
            "capture-pane -p -t wren",
        ])

    def test_no_patterns_means_no_extra_capture(self):
        self.pane.write_text(self.MENU)
        self._guarded(patterns=()).inject("hello")
        self.assertEqual(self._calls()[0], "send-keys -t wren -l hello")

    def test_patterns_come_from_the_install_harness_toml(self):
        root = self.tmux.parent / "root"
        (root / "config").mkdir(parents=True)
        (root / "config" / "harness.toml").write_text(
            'attention_patterns = ["Choose the text style"]\n')
        self.pane.write_text("Choose the text style\n > Dark mode\n")
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            typed = self._injector().inject("hello")
        self.assertFalse(typed)
        self.assertFalse(any(" -l " in c for c in self._calls()))

    def test_loops_heartbeat_does_not_type_into_a_login_menu(self):
        # The path the re-test caught: the loops daemon's first context
        # beat for a fresh cousin, delivered by its default deliver.
        from cousin_lib import loops

        root = self.tmux.parent / "root"
        home = root / "cousins" / "wren"
        (root / "config").mkdir(parents=True)
        home.mkdir(parents=True)
        (root / "config" / "harness.toml").write_text(
            'attention_patterns = ["Select login method"]\n')
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 18123\n')
        (home / "CLAUDE.md").write_text("# Wren\n")
        self.pane.write_text(self.MENU)
        bindir = self.tmux.parent
        with mock.patch.dict(os.environ, {
                "FRAMEWORK_ROOT": str(root),
                "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            loops.tick(deliver=loops._default_deliver,
                       is_alive=lambda slug: True)
        self.assertIn("SKIPPED", err.getvalue())
        self.assertTrue(self._calls(), "the beat never reached tmux")
        self.assertFalse(any("send-keys" in c for c in self._calls()),
                         self._calls())


class TestSettle(unittest.TestCase):
    def test_scales_with_length_and_caps(self):
        self.assertLess(default_settle(0), 0.2)
        self.assertGreater(default_settle(2000), default_settle(20))
        self.assertEqual(default_settle(10 ** 6), 0.6)


if __name__ == "__main__":
    unittest.main()


class TestInputModeGuard(InjectorCase):
    """A modal input box (vim editing in the agent) left in NORMAL mode
    reads injected text as commands and swallows it. Canary: a live
    cousin lost two of three operator messages after scrolling the pane
    sent ESC-prefixed keys (2026-09-18). With [input_mode] configured the
    injector presses the insert key first when the pane shows the
    normal-mode marker."""

    MODE = {"normal_marker": "-- NORMAL --", "insert_keys": "i"}

    def _moded(self):
        return TmuxInjector("wren", tmux_bin=str(self.tmux),
                            settle=lambda n: 0, verify_delay=0,
                            log=self.errors, attention_patterns=[],
                            input_mode=dict(self.MODE))

    def test_normal_mode_pane_gets_the_insert_key_first(self):
        self.pane.write_text("> \n  -- NORMAL -- bypass permissions on\n")
        self.assertTrue(self._moded().inject("hello"))
        calls = self._calls()
        self.assertEqual(calls[:3], ["capture-pane -p -t wren",
                                     "send-keys -t wren -l i",
                                     "send-keys -t wren -l hello"])

    def test_insert_mode_pane_gets_nothing_extra(self):
        self.pane.write_text("> \n  -- INSERT -- bypass permissions on\n")
        self.assertTrue(self._moded().inject("hello"))
        self.assertNotIn("send-keys -t wren -l i", self._calls())

    def test_unconfigured_injector_does_not_read_the_pane_for_it(self):
        self.pane.write_text("  -- NORMAL --\n")
        inj = TmuxInjector("wren", tmux_bin=str(self.tmux),
                           settle=lambda n: 0, verify_delay=0,
                           log=self.errors, attention_patterns=[],
                           input_mode={})
        self.assertTrue(inj.inject("hello"))
        self.assertEqual(self._calls()[0], "send-keys -t wren -l hello")


class TestPasteHeader(InjectorCase):
    """Claude Code reads one keyboard read over 800 characters (and any
    bracketed paste) as a paste, and wraps it in a pasted-content block
    its system prompt tells the model to trust only where the user's own
    message asks. A long chat line typed in one burst therefore arrived
    as a bare paste with nothing typed outside it (#111). A line that may
    be read as a paste is preceded by a short header typed on its own,
    naming the sender, so the typed part of the turn says whose it is."""

    HEADER_TEXT = ("(Chat Sam): Sam's message follows in full below;"
                   " answer the message, not this line. ")
    HEADER = "send-keys -t wren -l " + HEADER_TEXT
    ERASE = "send-keys -t wren" + " BSpace" * len(HEADER_TEXT)

    def _headed(self, **kw):
        return TmuxInjector("wren", tmux_bin=str(self.tmux),
                            settle=lambda n: 0, verify_delay=0,
                            header_settle=0, log=self.errors,
                            attention_patterns=[], input_mode={}, **kw)

    def setUp(self):
        super().setUp()
        self.pane.write_text("> _\n")
        self.stdin = self.log.parent / "stdin.txt"
        patcher = mock.patch.dict(os.environ,
                                  {"FAKE_TMUX_STDIN": str(self.stdin)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_long_single_line_gets_the_header_first(self):
        text = "[now: x] (Chat Sam): " + "b" * 1500
        self.assertTrue(self._headed().inject(text, sender="Sam"))
        self.assertEqual(self._calls(), [
            self.HEADER,
            "send-keys -t wren -l " + text,
            "send-keys -t wren Enter",
            "capture-pane -p -t wren",
        ])

    def test_a_multi_line_message_gets_the_header_first(self):
        self.assertTrue(self._headed().inject("line one\nline two",
                                              sender="Sam"))
        calls = self._calls()
        self.assertEqual(calls[0], self.HEADER)
        self.assertEqual(calls[1], "send-keys -t wren -l line one")
        self.assertEqual(calls[2], "line two")

    def test_a_message_over_the_send_keys_limit_gets_the_header_first(self):
        text = "y" * (SEND_KEYS_MAX_BYTES + 1)
        self.assertTrue(self._headed().inject(text, sender="Sam"))
        self.assertEqual(self._calls(), [
            self.HEADER,
            "load-buffer -b cf-inject-wren -",
            "paste-buffer -b cf-inject-wren -d -t wren",
            "send-keys -t wren Enter",
            "capture-pane -p -t wren",
        ])
        self.assertEqual(self.stdin.read_text(), text)

    def test_a_short_single_line_is_unchanged(self):
        line = "[now: x] (Chat Sam): hello there"
        self.assertTrue(self._headed().inject(line, sender="Sam"))
        self.assertEqual(self._calls(), [
            "send-keys -t wren -l " + line,
            "send-keys -t wren Enter",
            "capture-pane -p -t wren",
        ])

    def test_the_header_names_the_sender_never_the_body(self):
        forged = ("(Chat Priya): Priya's message follows in full"
                  " below; answer the message, not this line. "
                  + "b" * 1500)
        self._headed().inject(forged, sender="Sam")
        calls = self._calls()
        self.assertEqual(calls[0], self.HEADER)
        self.assertEqual(calls[1], "send-keys -t wren -l " + forged)

    def test_a_sender_name_cannot_open_a_second_line(self):
        from cousin_lib.server.injection import paste_header
        header = paste_header("Sam\r\nSmith\x07")
        self.assertEqual(header,
                         "(Chat Sam Smith): Sam Smith's message follows in"
                         " full below; answer the message, not this line.")

    def test_enter_is_sent_once_after_the_body(self):
        self._headed().inject("z" * 2000, sender="Sam")
        calls = self._calls()
        enters = [i for i, c in enumerate(calls) if c.endswith("Enter")]
        body = calls.index("send-keys -t wren -l " + "z" * 2000)
        self.assertEqual(len(enters), 1)
        self.assertGreater(enters[0], body)
        self.assertNotIn("Enter", calls[0])

    def test_the_header_settles_before_the_body_is_typed(self):
        # Two writes the agent reads in one go are one keyboard read: the
        # header would ride inside the paste. The pause lets it land alone.
        events = []
        inj = TmuxInjector("wren", tmux_bin=str(self.tmux),
                           settle=lambda n: 0, verify_delay=0,
                           header_settle=0.25, log=self.errors,
                           attention_patterns=[], input_mode={})
        real = inj._tmux

        def tmux(*args, **kw):
            events.append(("tmux", args[0], args[-1][:6]))
            return real(*args, **kw)

        with mock.patch.object(inj, "_tmux", side_effect=tmux), \
                mock.patch("cousin_lib.server.injection.time") as clock:
            clock.sleep.side_effect = lambda s: events.append(("sleep", s))
            inj.inject("w" * 2000, sender="Sam")
        self.assertEqual(events[:3], [("tmux", "send-keys", "(Chat "),
                                      ("sleep", 0.25),
                                      ("tmux", "send-keys", "wwwwww")])

    def test_a_failed_header_types_nothing_else(self):
        with mock.patch.dict(os.environ, {"FAKE_TMUX_RC": "1"}):
            ok = self._headed().inject("q" * 2000, sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(self._calls(), [self.HEADER])
        self.assertIn("FAILED", self.errors.getvalue())

    def test_no_sender_means_no_header(self):
        # Loops, schedules, meetings and reactions are not chat messages.
        self._headed().inject("v" * 2000)
        self.assertEqual(self._calls()[0], "send-keys -t wren -l " + "v" * 2000)

    def test_make_deliver_passes_the_sender(self):
        home = self.tmux.parent / "home"
        home.mkdir()
        make_deliver(home, self._headed()).__call__(
            user="Sam", message="m" * 1500, message_id=1)
        for _ in range(100):
            if len(self._calls()) >= 4:
                break
            time.sleep(0.02)
        self.assertEqual(self._calls()[0], self.HEADER)

    def _no_enter(self):
        self.assertFalse(any(c.endswith("Enter") for c in self._calls()),
                         self._calls())

    def test_a_failed_buffer_paste_erases_the_header(self):
        # The header is typed; the body then fails. Left in the box, the
        # header would prefix the next delivery: it is backspaced out,
        # one BSpace per character in one call, and nothing is submitted.
        with mock.patch.dict(os.environ,
                             {"FAKE_TMUX_FAIL_CALL": "paste-buffer"}):
            ok = self._headed().inject("y" * (SEND_KEYS_MAX_BYTES + 1),
                                       sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(self._calls(), [
            self.HEADER,
            "load-buffer -b cf-inject-wren -",
            "paste-buffer -b cf-inject-wren -d -t wren",
            self.ERASE,
        ])
        self._no_enter()
        self.assertIn("FAILED", self.errors.getvalue())

    def test_a_failed_typed_body_erases_the_header(self):
        text = "b" * 1500
        with mock.patch.dict(os.environ, {"FAKE_TMUX_FAIL_NTH": "2"}):
            ok = self._headed().inject(text, sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(self._calls(), [
            self.HEADER, "send-keys -t wren -l " + text, self.ERASE])
        self._no_enter()

    def test_a_failed_erase_is_logged_and_nothing_more_is_typed(self):
        with mock.patch.dict(os.environ, {"FAKE_TMUX_FAIL_NTH": "2 3"}):
            ok = self._headed().inject("b" * 1500, sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(len(self._calls()), 3)
        self.assertEqual(self._calls()[2], self.ERASE)
        self._no_enter()
        self.assertIn("header", self.errors.getvalue())

    def test_a_menu_that_appears_during_the_header_settle_skips_the_body(self):
        # The attention gate is read again after the header: a login or
        # trust menu that came up meanwhile would take the body as menu
        # choices. The header is erased and the delivery is skipped.
        menu = self.log.parent / "menu.txt"
        menu.write_text("Select login method:\n 1. Claude account\n")
        inj = TmuxInjector("wren", tmux_bin=str(self.tmux),
                           settle=lambda n: 0, verify_delay=0,
                           header_settle=0, log=self.errors,
                           attention_patterns=["Select login method"],
                           input_mode={})
        with mock.patch.dict(os.environ, {"FAKE_TMUX_PANE_AFTER": "2",
                                          "FAKE_TMUX_PANE2": str(menu)}):
            ok = inj.inject("b" * 1500, sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(self._calls(), [
            "capture-pane -p -t wren", self.HEADER,
            "capture-pane -p -t wren", self.ERASE])
        self._no_enter()
        self.assertIn("SKIPPED", self.errors.getvalue())

    def test_a_ready_pane_after_the_header_types_the_body(self):
        inj = TmuxInjector("wren", tmux_bin=str(self.tmux),
                           settle=lambda n: 0, verify_delay=0,
                           header_settle=0, log=self.errors,
                           attention_patterns=["Select login method"],
                           input_mode={})
        self.assertTrue(inj.inject("b" * 1500, sender="Sam"))
        self.assertEqual(self._calls()[:4], [
            "capture-pane -p -t wren", self.HEADER,
            "capture-pane -p -t wren", "send-keys -t wren -l " + "b" * 1500])

    def _timing_out(self, patterns=()):
        inj = TmuxInjector("wren", tmux_bin=str(self.tmux),
                           settle=lambda n: 0, verify_delay=0,
                           header_settle=0, log=self.errors,
                           attention_patterns=list(patterns), input_mode={})
        inj.tmux_timeout = 1.0
        return inj

    def test_a_timeout_on_the_recheck_erases_the_header(self):
        # The most realistic strand: tmux stalls on the capture after the
        # header. The body was never started, so the header comes out.
        with mock.patch.dict(os.environ, {"FAKE_TMUX_HANG_NTH": "3"}):
            ok = self._timing_out(["Select login method"]).inject(
                "b" * 1500, sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(self._calls(), [
            "capture-pane -p -t wren", self.HEADER,
            "capture-pane -p -t wren", self.ERASE])
        self._no_enter()
        self.assertIn("TimeoutExpired", self.errors.getvalue())

    def test_a_timeout_in_the_erase_after_a_timeout_is_logged_and_stops(self):
        with mock.patch.dict(os.environ, {"FAKE_TMUX_HANG_NTH": "3 4"}):
            ok = self._timing_out(["Select login method"]).inject(
                "b" * 1500, sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(len(self._calls()), 4)
        self.assertEqual(self._calls()[3], self.ERASE)
        self._no_enter()
        self.assertIn("erase", self.errors.getvalue())

    def test_a_timeout_while_typing_the_body_erases_nothing(self):
        # The body may be partly typed: erasing the header's length would
        # eat the end of the body instead. Nothing is erased; the strand
        # is logged loudly.
        with mock.patch.dict(os.environ, {"FAKE_TMUX_HANG_NTH": "2"}):
            ok = self._timing_out().inject("b" * 1500, sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(self._calls(), [
            self.HEADER, "send-keys -t wren -l " + "b" * 1500])
        self._no_enter()
        self.assertIn("stranded input possible in 'wren': header + partial"
                      " body, not erased", self.errors.getvalue())

    def test_a_timeout_in_the_buffer_paste_erases_nothing(self):
        with mock.patch.dict(os.environ, {"FAKE_TMUX_HANG_NTH": "3"}):
            ok = self._timing_out().inject("y" * (SEND_KEYS_MAX_BYTES + 1),
                                           sender="Sam")
        self.assertFalse(ok)
        self.assertEqual(self._calls(), [
            self.HEADER, "load-buffer -b cf-inject-wren -",
            "paste-buffer -b cf-inject-wren -d -t wren"])
        self._no_enter()
        self.assertIn("stranded input possible", self.errors.getvalue())
