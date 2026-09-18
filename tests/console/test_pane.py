"""The pane routes: capture, the SSE stream, input through the injection
lock, resize (docs/console-spec.md, "The pane"). tmux is a fake binary
that logs its argv; the stream's frame source is injectable."""
import json
import os
import pathlib
import stat
import tempfile
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

    def test_input_takes_the_injection_lock(self):
        from cousin_lib.server import injection
        seen = []

        def spy(*args, **kw):
            seen.append(injection._INJECT_LOCK.locked())
            return original(*args, **kw)

        original = pane.Tmux.run
        with mock.patch.object(pane.Tmux, "run", spy):
            self._post("/api/pane/input", cousin="testa", data="a")
        self.assertTrue(seen and all(seen[-1:]), seen)

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
