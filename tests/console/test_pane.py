"""The pane routes: capture, the SSE stream, input, resize
(docs/reference/console-api.md, "The pane"). tmux is a fake binary
that logs its argv; the stream's frame source is injectable."""
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from cousin_lib.console import pane, router, sse

_FAKE_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
case " $* " in
  *" has-session "*) exit "${FAKE_TMUX_HAS_SESSION:-0}" ;;
  *" capture-pane "*) cat "$FAKE_TMUX_PANE" 2>/dev/null ;;
  *" display-message "*)
    case "$*" in
      *alternate_on*) printf '%s\\n' "${FAKE_TMUX_STATE:-}" ;;
      *) printf '%s\\n' "${FAKE_TMUX_GEOM:-}" ;;
    esac ;;
esac
exit "${FAKE_TMUX_RC:-0}"
"""


class PaneCase(unittest.TestCase):
    def setUp(self):
        router.clear()
        pane.register()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        self.home.mkdir(parents=True)
        self._write_toml()
        self.tmux = self.root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.log = self.root / "tmux-calls.log"
        self.pane_file = self.root / "pane.txt"
        patcher = mock.patch.dict("os.environ", {
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(self.pane_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_toml(self, extra=""):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n[chat]\nport = 1\n'
            + extra)

    def _req(self, query=None, body=None):
        return SimpleNamespace(root=self.root, query=query or {},
                               body=body or {}, tmux_bin=str(self.tmux),
                               tmux_socket=None)

    def _get(self, path, **query):
        return router.dispatch("GET", path, req=self._req(query=query))

    def _post(self, path, **body):
        return router.dispatch("POST", path, req=self._req(body=body))

    def _calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class TestCapture(PaneCase):
    def test_capture_keeps_escapes_and_trims_trailing_blank_lines(self):
        self.pane_file.write_text("\x1b[1mhello\x1b[0m\nworld\n\n\x1b[0m \n\n")
        status, body = self._get("/api/pane", cousin="testa")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "\x1b[1mhello\x1b[0m\nworld")
        call = [c for c in self._calls() if "capture-pane" in c][0]
        self.assertIn("-p -e -t testa -S -200", call)

    def test_lines_parameter_reaches_tmux(self):
        self.pane_file.write_text("x")
        self._get("/api/pane", cousin="testa", lines="50")
        self.assertIn("-S -50", [c for c in self._calls()
                                 if "capture-pane" in c][0])

    def test_unknown_cousin_404_and_bad_slug_400(self):
        self.assertEqual(self._get("/api/pane", cousin="nobody")[0], 404)
        self.assertEqual(self._get("/api/pane", cousin="No Slug")[0], 400)
        self.assertEqual(self._get("/api/pane")[0], 400)

    def test_session_not_running_is_409(self):
        with mock.patch.dict("os.environ", {"FAKE_TMUX_HAS_SESSION": "1"}):
            status, body = self._get("/api/pane", cousin="testa")
        self.assertEqual(status, 409)
        self.assertIn("error", body)

    def test_no_tmux_session_configured_is_400(self):
        self._write_toml('tmux_session = ""\n')
        status, _ = self._get("/api/pane", cousin="testa")
        self.assertEqual(status, 400)

    def test_configured_session_name_is_the_target(self):
        self._write_toml('tmux_session = "other"\n')
        self.pane_file.write_text("x")
        self._get("/api/pane", cousin="testa")
        self.assertIn("-t other", [c for c in self._calls()
                                   if "capture-pane" in c][0])

    def test_socket_flag_is_passed_when_configured(self):
        self.pane_file.write_text("x")
        req = self._req(query={"cousin": "testa"})
        req.tmux_socket = "/tmp/sock.example"
        router.dispatch("GET", "/api/pane", req=req)
        self.assertTrue(all(c.startswith("-S /tmp/sock.example ")
                            for c in self._calls()), self._calls())


class TestInputTokens(unittest.TestCase):
    def test_printable_runs_and_named_keys(self):
        self.assertEqual(pane.input_tokens("ab\r"),
                         [("literal", "ab"), ("key", "Enter")])
        self.assertEqual(pane.input_tokens("\x7f\t"),
                         [("key", "BSpace"), ("key", "Tab")])
        self.assertEqual(pane.input_tokens("\x03"), [("key", "C-c")])
        self.assertEqual(pane.input_tokens("\x1b[A\x1b[3~\x1bOP"),
                         [("key", "Up"), ("key", "DC"), ("key", "F1")])
        self.assertEqual(pane.input_tokens("\x1b[H\x1b[F"),
                         [("key", "Home"), ("key", "End")])

    def test_bare_escape_is_the_escape_key(self):
        self.assertEqual(pane.input_tokens("\x1b"), [("key", "Escape")])
        self.assertEqual(pane.input_tokens("\x1bx"),
                         [("key", "Escape"), ("literal", "x")])

    def test_unknown_csi_and_ss3_are_dropped_never_forwarded_as_escape(self):
        self.assertEqual(pane.input_tokens("\x1b[I"), [])
        self.assertEqual(pane.input_tokens("\x1b[?1;2c"), [])
        self.assertEqual(pane.input_tokens("\x1bOZ"), [])
        self.assertEqual(pane.input_tokens("a\x1b[Ob"),
                         [("literal", "a"), ("literal", "b")])

    def test_sgr_mouse_reports_are_mouse_tokens(self):
        self.assertEqual(pane.input_tokens("\x1b[<64;10;5M"),
                         [("mouse", "\x1b[<64;10;5M")])
        self.assertEqual(pane.input_tokens("\x1b[<0;1;1m"),
                         [("mouse", "\x1b[<0;1;1m")])

    def test_literal_chunking_respects_the_limit(self):
        self.assertEqual(pane.chunk_literal("abcdef", 4), ["abcd", "ef"])
        self.assertEqual(pane.chunk_literal("", 4), [])


class TestInput(PaneCase):
    def test_input_sends_literals_with_l_and_double_dash(self):
        status, body = self._post("/api/pane/input", cousin="testa",
                                  data="-x\r")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "tokens": 2})
        sends = [c for c in self._calls() if "send-keys" in c]
        self.assertEqual(sends, ["send-keys -t testa -l -- -x",
                                 "send-keys -t testa Enter"])

    def test_input_errors(self):
        self.assertEqual(self._post("/api/pane/input", data="a")[0], 400)
        self.assertEqual(self._post("/api/pane/input", cousin="nobody",
                                    data="a")[0], 404)
        with mock.patch.dict("os.environ", {"FAKE_TMUX_HAS_SESSION": "1"}):
            self.assertEqual(self._post("/api/pane/input", cousin="testa",
                                        data="a")[0], 409)

    def test_tmux_failure_is_500_with_its_stderr(self):
        with mock.patch.dict("os.environ", {"FAKE_TMUX_RC": "3"}):
            with mock.patch.object(pane.Tmux, "has_session",
                                   return_value=True):
                status, body = self._post("/api/pane/input", cousin="testa",
                                          data="a")
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])

    def test_mouse_reports_reach_a_program_tracking_the_mouse(self):
        with mock.patch.dict("os.environ",
                             {"FAKE_TMUX_STATE": "1 1 1 80 24 0 0 0"}):
            status, body = self._post("/api/pane/input", cousin="testa",
                                      data="\x1b[<64;3;4M\x1b[<65;3;4M")
        self.assertEqual((status, body["tokens"]), (200, 2))
        sends = [c for c in self._calls() if "send-keys" in c]
        self.assertEqual(sends, ["send-keys -t testa -l -- \x1b[<64;3;4M",
                                 "send-keys -t testa -l -- \x1b[<65;3;4M"])

    def test_mouse_reports_are_dropped_when_the_program_does_not_track(self):
        # an SGR report typed into a program not tracking the mouse is
        # an Escape followed by text: a modal editor leaves insert mode
        for st in ("0 0 0 80 24 0 0 1", "1 1 0 80 24 0 0 1", ""):
            self.log.write_text("")
            with mock.patch.dict("os.environ", {"FAKE_TMUX_STATE": st}):
                status, body = self._post("/api/pane/input", cousin="testa",
                                          data="a\x1b[<64;3;4Mb")
            self.assertEqual((status, body["tokens"]), (200, 2), st)
            sends = [c for c in self._calls() if "send-keys" in c]
            self.assertEqual(sends, ["send-keys -t testa -l -- a",
                                     "send-keys -t testa -l -- b"], st)

    def test_no_state_query_without_a_mouse_report(self):
        self._post("/api/pane/input", cousin="testa", data="ab")
        self.assertFalse([c for c in self._calls() if "display-message" in c])

    def test_empty_data_is_a_noop_200(self):
        status, body = self._post("/api/pane/input", cousin="testa", data="")
        self.assertEqual((status, body["tokens"]), (200, 0))


class TestResize(PaneCase):
    def test_resize_clamps_and_targets_window_zero(self):
        status, body = self._post("/api/pane/resize", cousin="testa",
                                  cols=1000, rows=2)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "cols": 400, "rows": 5})
        call = [c for c in self._calls() if "resize-window" in c][0]
        self.assertEqual(call, "resize-window -t testa:0 -x 400 -y 5")

    def test_non_integer_geometry_is_400(self):
        self.assertEqual(self._post("/api/pane/resize", cousin="testa",
                                    cols="wide", rows=10)[0], 400)
        self.assertEqual(self._post("/api/pane/resize", cousin="testa",
                                    cols=True, rows=10)[0], 400)


class TestTmuxKind(PaneCase):
    """A tmux-kind runner cousin (phase 11): its pane is on the framework's
    own socket under the runner's session name, not the legacy address.
    A person may type into it only while it waits on a person (the trust,
    login, onboarding, bypass or MCP dialog), where the runner never
    types; it keeps its fixed size."""

    TRUST = "Quick safety check\nIs this a project you trust?\n> 1. Yes, I trust this folder\n"
    PROMPT = "the model's words\n" + "\u2500" * 20 + "\n\u276f \n" + "\u2500" * 20 + "\n"

    def setUp(self):
        super().setUp()
        self._write_toml('\n[agent]\nrunner = "tmux"\n')
        self.socket = str(self.root / "run" / "tmux.sock")

    def test_it_reads_the_kinds_socket_and_exact_session(self):
        self.pane_file.write_text("hello")
        status, body = self._get("/api/pane", cousin="testa")
        self.assertEqual(status, 200, body)
        calls = self._calls()
        self.assertTrue(all(c.startswith("-S %s " % self.socket) for c in calls), calls)
        self.assertIn("has-session -t =tmux-testa", calls[0])
        self.assertIn("-t =tmux-testa: -S -200", [c for c in calls if "capture-pane" in c][0])

    def _sends(self):
        return [c.split(" send-keys ", 1)[1] for c in self._calls() if " send-keys " in c]

    def test_keys_go_in_on_a_screen_that_waits_on_a_person(self):
        self.pane_file.write_text(self.TRUST)
        status, body = self._post("/api/pane/input", cousin="testa", data="\x1b[B1\r")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["tokens"], 3)
        self.assertEqual(self._sends(), ["-t =tmux-testa: Down", "-t =tmux-testa: -l -- 1",
                                         "-t =tmux-testa: Enter"])
        self.assertTrue(all(c.startswith("-S %s " % self.socket) for c in self._calls()))

    def test_keys_are_refused_where_the_runner_types(self):
        self.pane_file.write_text(self.PROMPT)
        status, body = self._post("/api/pane/input", cousin="testa", data="\r")
        self.assertEqual(status, 409, body)
        self.assertIn("waits on a person", body["error"])
        self.assertEqual(self._sends(), [])

    def test_only_a_closed_key_set_goes_in_and_nothing_else_is_sent(self):
        self.pane_file.write_text(self.TRUST)
        for data in ("hello", "yes", "\x03", "\x1b[<64;3;4M", "\x1b[H", "1" * 50 + "\r", "\x1b[Bab"):
            self.log.write_text("")
            status, body = self._post("/api/pane/input", cousin="testa", data=data)
            self.assertEqual(status, 409, (data, body))
            self.assertEqual(body["sent"], 0, data)
            self.assertEqual(self._sends(), [], data)
            self.assertNotIn(data, body["error"])            # a paste is never echoed
        for data in ("y", "n", "7", "\t", "\x7f", "\x1b[A", "\x1b[C", "\x1b[D"):
            status, body = self._post("/api/pane/input", cousin="testa", data=data)
            self.assertEqual(status, 200, (data, body))

    def test_a_request_ends_at_its_first_enter(self):
        """The reviewer's case: one POST of "\\rhello\\r" answered the trust
        screen, then typed and submitted `hello` into the prompt."""
        self.pane_file.write_text(self.TRUST)
        status, body = self._post("/api/pane/input", cousin="testa", data="\rhello\r")
        self.assertEqual(status, 409, body)
        self.assertEqual((body["sent"], body["refused"]), (1, 2))
        self.assertIn("1 key went in", body["error"])
        self.assertEqual(self._sends(), ["-t =tmux-testa: Enter"])
        status, body = self._post("/api/pane/input", cousin="testa", data="\x1b1")
        self.assertEqual((status, body["sent"]), (409, 0))       # Escape ends one too...
        self.assertIn("after Enter", body["error"])              # ...and Enter was just now

    def test_input_waits_out_the_screen_change_after_an_enter(self):
        self.pane_file.write_text(self.TRUST)
        self.assertEqual(self._post("/api/pane/input", cousin="testa", data="\r")[0], 200)
        status, body = self._post("/api/pane/input", cousin="testa", data="\r")
        self.assertEqual(status, 409, body)
        self.assertIn("after Enter", body["error"])
        with mock.patch.object(pane, "ENTER_SETTLE_S", 0.0):
            self.assertEqual(self._post("/api/pane/input", cousin="testa", data="\r")[0], 200)

    def test_the_screen_is_read_again_before_each_key(self):
        self.pane_file.write_text(self.TRUST)
        screens = iter(["trust", None])
        with mock.patch.object(pane, "_answerable_screen", lambda tmux, session: next(screens)):
            status, body = self._post("/api/pane/input", cousin="testa", data="\x1b[B\x1b[B")
        self.assertEqual(status, 409, body)
        self.assertEqual((body["sent"], body["refused"]), (1, 1))
        self.assertEqual(self._sends(), ["-t =tmux-testa: Down"])

    def test_check_and_send_hold_the_per_pane_lock(self):
        self.pane_file.write_text(self.TRUST)
        seen = []
        original = pane._answerable_screen

        def spy(tmux, session):
            seen.append(any(lock.locked() for lock in pane._kind_locks(None).values()))
            return original(tmux, session)
        with mock.patch.object(pane, "_answerable_screen", spy):
            self._post("/api/pane/input", cousin="testa", data="1")
        self.assertEqual(seen, [True])

    def test_login_and_onboarding_are_left_to_a_terminal(self):
        self.pane_file.write_text("Select login method:\n 1. Claude account\n")
        status, body = self._post("/api/pane/input", cousin="testa", data="1")
        self.assertEqual(status, 409, body)
        self.assertIn("in a terminal", body["error"])

    def test_an_unreadable_screen_refuses_the_keys(self):
        with mock.patch.dict("os.environ", {"FAKE_TMUX_RC": "1", "FAKE_TMUX_HAS_SESSION": "0"}):
            status, body = self._post("/api/pane/input", cousin="testa", data="a")
        self.assertEqual(status, 409, body)
        self.assertEqual(self._sends(), [])

    def test_it_keeps_its_fixed_size(self):
        status, body = self._post("/api/pane/resize", cousin="testa", cols=100, rows=30)
        self.assertEqual(status, 409, body)
        self.assertIn("fixed size", body["error"])
        self.assertFalse([c for c in self._calls() if "resize-window" in c])

    def test_an_sdk_runner_keeps_the_legacy_address(self):
        self._write_toml('\n[agent]\nrunner = "sdk"\n')
        self.pane_file.write_text("x")
        self._get("/api/pane", cousin="testa")
        self.assertIn("-t testa", [c for c in self._calls() if "capture-pane" in c][0])



_TRUST_PROGRAM = r"""
import pathlib, sys
log = pathlib.Path(sys.argv[1])
print("Quick safety check")
print("Is this a project you trust?")
print("> 1. Yes, I trust this folder")
print("  2. No, exit", flush=True)
line = sys.stdin.readline()
log.write_text("trust:%r\n" % line)
print("\x1b[2J\x1b[H" + "\u2500" * 20 + "\n\u276f \n" + "\u2500" * 20, flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    with log.open("a") as fh:
        fh.write("prompt:%r\n" % line)
"""


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TestTmuxKindOnARealServer(unittest.TestCase):
    """The reviewer's reproduction on a scratch tmux server: one POST of
    "\\rhello\\r" must answer the trust screen and type nothing into the
    prompt that follows it."""

    def setUp(self):
        router.clear()
        pane.register()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        home = self.root / "cousins" / "testa"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\n[chat]\nport = 1\n'
            '\n[agent]\nrunner = "tmux"\n')
        (self.root / "run").mkdir()
        self.sock = str(self.root / "run" / "tmux.sock")
        self.log = self.root / "typed.log"
        script = self.root / "trust.py"
        script.write_text(_TRUST_PROGRAM)
        subprocess.run(["tmux", "-f", "/dev/null", "-S", self.sock, "new-session", "-d",
                        "-s", "tmux-testa", "-x", "200", "-y", "50",
                        "%s %s %s" % (sys.executable, script, self.log)], check=True)
        self.addCleanup(subprocess.run, ["tmux", "-S", self.sock, "kill-server"],
                        capture_output=True)
        self.wait_for(lambda: "Quick safety check" in self.screen())

    def screen(self):
        return subprocess.run(["tmux", "-S", self.sock, "capture-pane", "-p", "-t", "=tmux-testa:"],
                              capture_output=True, text=True).stdout

    def wait_for(self, cond, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        self.fail("timed out; the screen:\n" + self.screen())

    def post(self, data):
        req = SimpleNamespace(root=self.root, query={}, body={"cousin": "testa", "data": data},
                              tmux_bin="tmux", tmux_socket=None)
        return router.dispatch("POST", "/api/pane/input", req=req)

    def test_enter_then_hello_answers_the_dialog_and_types_nothing_more(self):
        status, body = self.post("\rhello\r")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["sent"], 1)
        self.wait_for(lambda: self.log.exists() and "trust:" in self.log.read_text())
        self.wait_for(lambda: "\u276f" in self.screen())
        time.sleep(0.3)
        self.assertNotIn("hello", self.log.read_text())
        self.assertNotIn("hello", self.screen())
        status, body = self.post("hello\r")                     # the prompt is the runner's
        self.assertEqual(status, 409, body)
        time.sleep(0.2)
        self.assertNotIn("prompt:", self.log.read_text())


def _state(**kw):
    base = {"alt": 0, "mouse": 0, "sgr": 0, "cols": 80, "rows": 4,
            "cx": 0, "cy": 0, "cursor": 1}
    base.update(kw)
    return base


class TestComposeFrame(unittest.TestCase):
    """One frame = the capture, trimmed, then the control tail: mouse
    mode, cursor placement relative to the frame's last line, cursor
    visibility. The tail is what the browser terminal is left in after
    it resets and writes the frame."""

    def test_no_state_trims_trailing_blank_lines_only(self):
        text, top = pane.compose_frame("a\nb\n\n\x1b[0m \n", None)
        self.assertEqual((text, top), ("a\nb", 0))

    def test_normal_buffer_with_history_cursor_on_last_line(self):
        raw = "h1\nh2\nr0\nr1\n\n\n"          # 2 history + 4 rows
        text, top = pane.compose_frame(raw, _state(cy=1, cx=3))
        self.assertEqual(top, 2)
        self.assertEqual(text, "h1\nh2\nr0\nr1\x1b[4G\x1b[?25h")

    def test_cursor_on_a_blank_line_below_the_content_keeps_that_line(self):
        raw = "h1\nh2\nr0\nr1\n\n\n"
        text, _ = pane.compose_frame(raw, _state(cy=3, cx=0))
        self.assertEqual(text, "h1\nh2\nr0\nr1\n\n\x1b[1G\x1b[?25h")

    def test_cursor_above_the_last_line_moves_up_and_can_hide(self):
        # a full-screen program: input line with a footer under it
        raw = "top\n> typed\nfooter\nstatus\n"
        text, top = pane.compose_frame(
            raw, _state(alt=1, mouse=1, sgr=1, cy=1, cx=7, cursor=0))
        self.assertEqual(top, 0)
        self.assertEqual(
            text, "top\n> typed\nfooter\nstatus"
                  "\x1b[?1000h\x1b[?1006h\x1b[2A\x1b[8G\x1b[?25l")

    def test_mouse_mode_needs_the_sgr_encoding_the_input_path_forwards(self):
        text, _ = pane.compose_frame("x\n\n\n\n",
                                     _state(mouse=1, sgr=0))
        self.assertNotIn("?1000h", text)
        self.assertTrue(text.endswith("\x1b[1G\x1b[?25h"))

    def test_capture_shorter_than_the_screen_places_no_cursor(self):
        text, top = pane.compose_frame("x\n", _state(rows=4, cy=2))
        self.assertEqual((text, top), ("x\x1b[?25h", 0))


class TestPaneState(PaneCase):
    def test_state_reads_one_display_message(self):
        with mock.patch.dict("os.environ",
                             {"FAKE_TMUX_STATE": "1 1 1 120 40 2 37 0"}):
            st = pane.Tmux(str(self.tmux)).state("testa")
        self.assertEqual(st, {"alt": 1, "mouse": 1, "sgr": 1, "cols": 120,
                              "rows": 40, "cx": 2, "cy": 37, "cursor": 0})
        call = [c for c in self._calls() if "display-message" in c][0]
        for fmt in ("#{alternate_on}", "#{mouse_any_flag}",
                    "#{mouse_sgr_flag}", "#{cursor_flag}"):
            self.assertIn(fmt, call)

    def test_state_is_none_when_tmux_prints_nothing_usable(self):
        with mock.patch.dict("os.environ", {"FAKE_TMUX_STATE": "garbage"}):
            self.assertIsNone(pane.Tmux(str(self.tmux)).state("testa"))
        self.assertIsNone(pane.Tmux(str(self.tmux)).state("testa"))

    def test_stream_route_frame_carries_the_live_state(self):
        self.pane_file.write_text("a\nb\n")
        with mock.patch.dict("os.environ",
                             {"FAKE_TMUX_STATE": "1 1 1 80 2 1 1 0"}):
            _, body = self._get("/api/pane/stream", cousin="testa")
            first = _events([next(iter(body))])[0]
            body.close()
        self.assertEqual(first[1]["state"]["mouse"], 1)
        self.assertTrue(first[1]["text"].endswith(
            "\x1b[?1000h\x1b[?1006h\x1b[2G\x1b[?25l"),
            repr(first[1]["text"]))


def _events(chunks):
    """Parse raw SSE bytes into (event, data) pairs; comments as
    (None, text)."""
    out = []
    for raw in b"".join(chunks).split(b"\n\n"):
        if not raw:
            continue
        text = raw.decode()
        if text.startswith(":"):
            out.append((None, text))
            continue
        event, data = None, None
        for line in text.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((event, data))
    return out


class TestStream(PaneCase):
    def _stream(self, frames, geoms=None, states=None, **kw):
        frames = list(frames)
        geoms = list(geoms or [(80, 24)])
        clock = {"t": 0.0}

        def capture(lines):
            return frames.pop(0) if len(frames) > 1 else frames[0]

        def geometry():
            return geoms.pop(0) if len(geoms) > 1 else geoms[0]

        def sleep(s):
            clock["t"] += s

        gen = pane.stream_pane(
            "testa", 200, tmux=pane.Tmux(str(self.tmux)), capture=capture,
            geometry=geometry, state=lambda: states,
            clock=lambda: clock["t"], sleep=sleep, **kw)
        return gen, clock

    def test_first_frame_carries_the_pane_state_and_a_cursor_tail(self):
        state = {"alt": 1, "mouse": 1, "sgr": 1, "cols": 80, "rows": 3,
                 "cx": 5, "cy": 1, "cursor": 1, "top": 0}
        gen, _ = self._stream(["one\ntwo\nthree\n"], states=state)
        first = _events([next(gen)])[0]
        gen.close()
        self.assertEqual(first[0], "pane")
        self.assertTrue(first[1]["changed"])
        self.assertIn("ts", first[1])
        self.assertTrue(first[1]["text"].startswith("one\ntwo\nthree"))
        # the cursor is one line above the frame's last line, column 6
        self.assertTrue(first[1]["text"].endswith(
            "\x1b[?1000h\x1b[?1006h\x1b[1A\x1b[6G\x1b[?25h"),
            repr(first[1]["text"]))
        self.assertEqual(first[1]["state"]["alt"], 1)
        self.assertEqual(first[1]["state"]["mouse"], 1)
        self.assertEqual(first[1]["state"]["top"], 0)

    def test_a_frame_without_state_is_the_trimmed_capture(self):
        gen, _ = self._stream(["one\n\n"], states=None)
        first = _events([next(gen)])[0]
        gen.close()
        self.assertEqual(first[1]["text"], "one")
        self.assertIsNone(first[1]["state"])

    def test_unchanged_frames_tick_then_heartbeat_after_three_seconds(self):
        gen, clock = self._stream(["same"], poll=0.5)
        chunks = [next(gen) for _ in range(9)]
        gen.close()
        parsed = _events(chunks)
        self.assertEqual(parsed[0][0], "pane")
        self.assertEqual(parsed[1], (None, ": tick"))
        kinds = [p[0] for p in parsed]
        self.assertIn("heartbeat", kinds)
        self.assertLess(kinds.index("heartbeat"), 9)
        self.assertNotIn("delta", kinds)

    def test_changed_frame_is_resent_as_a_pane_event(self):
        gen, _ = self._stream(["a", "a", "b"])
        chunks = [next(gen) for _ in range(4)]
        gen.close()
        panes = [p for p in _events(chunks) if p[0] == "pane"]
        self.assertEqual([p[1]["text"] for p in panes], ["a", "b"])

    def test_geometry_change_emits_geom_then_a_fresh_pane(self):
        gen, clock = self._stream(["a"], geoms=[(80, 24), (100, 30)],
                                  poll=0.5, geom_every=2.0)
        chunks = [next(gen) for _ in range(8)]
        gen.close()
        parsed = [p for p in _events(chunks) if p[0] is not None]
        kinds = [p[0] for p in parsed]
        self.assertIn("geom", kinds)
        i = kinds.index("geom")
        self.assertEqual(parsed[i][1]["cols"], 100)
        self.assertEqual(parsed[i][1]["rows"], 30)
        self.assertEqual(kinds[i + 1], "pane")

    def test_close_stops_the_generator_cleanly(self):
        gen, _ = self._stream(["a"])
        next(gen)
        gen.close()
        with self.assertRaises(StopIteration):
            next(gen)

    def test_stream_route_returns_a_stream_after_validating(self):
        self.pane_file.write_text("hello")
        status, body = self._get("/api/pane/stream", cousin="testa")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, sse.Stream)
        self.assertEqual(body.content_type, "text/event-stream")
        headers = {k.lower(): v for k, v in body.headers}
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(headers["x-accel-buffering"], "no")
        first = _events([next(iter(body))])[0]
        body.close()
        self.assertEqual(first[0], "pane")
        self.assertTrue(first[1]["text"].startswith("hello"))

    def test_stream_route_errors_are_json_not_streams(self):
        self.assertEqual(self._get("/api/pane/stream", cousin="nobody")[0],
                         404)
        with mock.patch.dict("os.environ", {"FAKE_TMUX_HAS_SESSION": "1"}):
            status, body = self._get("/api/pane/stream", cousin="testa")
        self.assertEqual(status, 409)
        self.assertNotIsInstance(body, sse.Stream)


if __name__ == "__main__":
    unittest.main()
