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
    TmuxInjector,
    compose_delivery,
    default_settle,
    make_deliver,
)

_FAKE_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
for a in "$@"; do
  if [ "$a" = capture-pane ]; then cat "$FAKE_TMUX_PANE" 2>/dev/null; fi
  if [ "$a" = -l ]; then sleep "${FAKE_TMUX_PASTE_DELAY:-0}"; fi
done
exit "${FAKE_TMUX_RC:-0}"
"""


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
