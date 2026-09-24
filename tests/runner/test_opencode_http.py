"""opencode_http: the `opencode serve` child, the v1 HTTP client and the
SSE reader (R1, R3), against the fake server (R15). The real binary is
opt-in: COUSIN_LIVE_OPENCODE=1 with OPENCODE_BIN=<path>."""
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from cousin_lib.runner import opencode_http
from cousin_lib.runner.opencode_http import (EventReader, OpencodeClient, OpencodeError,
                                             OpencodeServer, iter_sse)
from tests._hermetic import HermeticCase
from tests.runner._fake_opencode import FakeOpencode

FAKE = Path(__file__).with_name("_fake_opencode.py")
PASSWORD = "testa-pw"


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _zombie(pid):
    """Dead and waiting to be reaped by its parent (a test's own child)."""
    try:
        stat_line = Path("/proc/%d/stat" % pid).read_text()
    except OSError:
        return True
    return stat_line[stat_line.rindex(")") + 2:].split()[0] == "Z"


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class ServerCase(HermeticCase):
    """A fake `opencode` binary: a sh wrapper that runs _fake_opencode.py."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bin = self.dir / "opencode"
        self.bin.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.config = self.dir / "opencode.runner.json"
        self.config.write_text(json.dumps({"model": "local/m1",
                                           "disabled_providers": ["opencode"]}))
        self.dump = self.dir / "dump.json"

    def env(self, **extra):
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.home),
               "FAKE_OPENCODE_ENV_DUMP": str(self.dump)}
        env.update(extra)
        return env

    def server(self, timeout=10, **extra):
        srv = OpencodeServer(str(self.bin), cwd=self.home, env=self.env(**extra),
                             config_path=self.config, timeout=timeout)
        self.addCleanup(srv.stop)
        return srv

    def seen(self):
        return json.loads(self.dump.read_text())


class TestOpencodeServer(ServerCase):
    def test_it_serves_on_loopback_with_a_fresh_password(self):
        srv = self.server().start()
        self.assertTrue(srv.url.startswith("http://127.0.0.1:"))
        self.assertEqual(OpencodeClient(srv.url, srv.password).health(),
                         {"healthy": True, "version": "1.18.31"})
        seen = self.seen()
        self.assertEqual(seen["argv"], ["serve", "--hostname", "127.0.0.1",
                                        "--port", str(srv.port)])
        self.assertEqual(seen["cwd"], str(self.home))
        self.assertEqual(seen["pgid"], seen["pid"])      # its own process group
        env = seen["env"]
        self.assertEqual(env["OPENCODE_SERVER_PASSWORD"], srv.password)
        self.assertGreaterEqual(len(srv.password), 32)
        self.assertEqual(env["OPENCODE_CONFIG"], str(self.config))
        for name in ("OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_SHARE",
                     "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_PROJECT_CONFIG"):
            self.assertEqual(env[name], "1", name)
        self.assertEqual(OpencodeClient(srv.url, srv.password).config()["model"], "local/m1")
        with self.assertRaises(OpencodeError) as err:
            OpencodeClient(srv.url, "not-the-password").health()
        self.assertEqual(err.exception.status, 401)

    def test_each_start_has_its_own_password(self):
        one, two = self.server().start(), self.server().start()
        self.assertNotEqual(one.password, two.password)
        self.assertNotEqual(one.port, two.port)

    def test_the_environment_is_scrubbed_and_cannot_override_it(self):
        srv = self.server(ANTHROPIC_API_KEY="sk-ant-wren", CLAUDE_CODE_OAUTH_TOKEN="tok",
                          ANTHROPIC_BASE_URL="http://127.0.0.1:3456",
                          OPENCODE_SERVER_PASSWORD="stale", OPENCODE_SERVER_USERNAME="mallory",
                          OPENCODE_CONFIG="/elsewhere.json",
                          OPENCODE_CONFIG_CONTENT='{"plugin": ["x"]}',
                          OPENCODE_CONFIG_DIR="/elsewhere", XDG_DATA_HOME="/d").start()
        env = self.seen()["env"]
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
                     "OPENCODE_SERVER_USERNAME", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR"):
            self.assertNotIn(name, env)
        self.assertEqual(env["OPENCODE_SERVER_PASSWORD"], srv.password)
        self.assertEqual(env["OPENCODE_CONFIG"], str(self.config))
        self.assertEqual((env["HOME"], env["XDG_DATA_HOME"]), (str(self.home), "/d"))

    def test_stop_terminates_it(self):
        srv = self.server().start()
        pid = srv.pid
        self.assertTrue(srv.alive())
        srv.stop()
        self.assertFalse(srv.alive())
        self.assertTrue(_gone(pid))
        srv.stop()                                       # idempotent

    def test_stop_kills_one_that_ignores_sigterm(self):
        srv = self.server(FAKE_OPENCODE_MODE="ignore-term").start()
        pid = srv.pid
        t = time.monotonic()
        srv.stop(timeout=0.5)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertTrue(_gone(pid))

    def test_the_health_wait_is_bounded(self):
        srv = self.server(timeout=1, FAKE_OPENCODE_MODE="mute")
        t = time.monotonic()
        with self.assertRaises(OpencodeError) as err:
            srv.start()
        self.assertLess(time.monotonic() - t, 4.0)
        self.assertIn("healthy", str(err.exception))
        self.assertTrue(_wait(lambda: _gone(self.seen()["pid"])))

    def test_a_child_that_exits_is_an_error_with_its_output(self):
        srv = self.server(FAKE_OPENCODE_MODE="exit")
        t = time.monotonic()
        with self.assertRaises(OpencodeError) as err:
            srv.start()
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertIn("refuses to start", str(err.exception))

    def test_a_missing_binary_is_an_error(self):
        srv = OpencodeServer(str(self.dir / "no-such-opencode"), cwd=self.home, env=self.env(),
                             config_path=self.config)
        with self.assertRaises(OpencodeError) as err:
            srv.start()
        self.assertIn("no-such-opencode", str(err.exception))


class TestOrphans(ServerCase):
    """Review Important 2: `opencode serve` runs in its own session, so the
    supervisor's SIGKILL of the runner's process group missed it and it
    kept running (a turn, provider calls, bash) after the runner died."""

    def killpg_later(self, pid):
        def kill():
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.addCleanup(kill)

    def test_a_sigkilled_runner_takes_its_opencode_serve_with_it(self):
        """A real runner stand-in: a Python process that starts the server,
        prints the child's pid, then waits; SIGKILL it, as the supervisor's
        escalation or the OOM killer would."""
        script = (
            "import json, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from cousin_lib.runner.opencode_http import OpencodeServer\n"
            "s = OpencodeServer(%r, cwd=%r, env=json.loads(sys.argv[1]), config_path=%r,"
            " timeout=10).start()\n"
            "print(s.pid, flush=True)\n"
            "time.sleep(60)\n"
            % (str(Path(__file__).resolve().parents[2]), str(self.bin), str(self.home),
               str(self.config)))
        runner = subprocess.Popen([sys.executable, "-c", script, json.dumps(self.env())],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(runner.wait)
        child = int(runner.stdout.readline())
        self.killpg_later(child)
        self.assertFalse(_gone(child))
        runner.kill()                                      # SIGKILL, no teardown
        self.assertTrue(_wait(lambda: _gone(child) or _zombie(child), 5),
                        "opencode serve %d outlived its runner" % child)

    def test_a_leftover_server_named_by_the_pidfile_is_killed(self):
        srv = self.server().start()
        self.killpg_later(srv.pid)
        pidfile = self.dir / "opencode.pid"
        opencode_http.write_pidfile(pidfile, srv.pid)
        self.assertEqual(os.stat(pidfile).st_mode & 0o777, 0o600)
        self.assertEqual(opencode_http.reap_leftover(pidfile), [srv.pid])
        self.assertTrue(_wait(lambda: _gone(srv.pid) or _zombie(srv.pid)))
        self.assertFalse(pidfile.exists())
        self.assertEqual(opencode_http.reap_leftover(pidfile), [])   # nothing left

    def detached_child(self, srv):
        path = self.dir / "detached.pid"
        self.assertTrue(_wait(lambda: path.exists() and path.read_text().strip()))
        child = int(path.read_text())
        def kill():
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(kill)
        self.assertNotEqual(os.getpgid(child), srv.pid)        # its own session
        return child

    def test_stop_kills_what_the_server_started_in_a_session_of_its_own(self):
        """Review round 2, minor 1: opencode starts the model's bash in a
        session of its own (measured on 1.18.31), which the server's process
        group never held. Every process carrying this start's marker goes."""
        srv = self.server(FAKE_OPENCODE_DETACH=str(self.dir / "detached.pid")).start()
        child = self.detached_child(srv)
        srv.stop()
        self.assertTrue(_wait(lambda: _gone(child) or _zombie(child)), "the child outlived stop")

    def test_a_leftover_is_reaped_with_its_detached_children_even_without_its_leader(self):
        srv = self.server(FAKE_OPENCODE_DETACH=str(self.dir / "detached.pid")).start()
        self.killpg_later(srv.pid)
        child = self.detached_child(srv)
        pidfile = self.dir / "opencode.pid"
        opencode_http.write_pidfile(pidfile, srv.pid, marker=srv.marker)
        record = json.loads(pidfile.read_text())
        self.assertEqual(record["boot_id"], Path("/proc/sys/kernel/random/boot_id")
                         .read_text().strip())
        os.killpg(srv.pid, signal.SIGKILL)                    # the leader is gone (PDEATHSIG)
        srv.proc.wait(5)
        self.assertFalse(_gone(child))
        killed = opencode_http.reap_leftover(pidfile)
        self.assertEqual(killed, [child])
        self.assertTrue(_wait(lambda: _gone(child) or _zombie(child)))

    def test_the_marker_sweep_repeats_until_a_pass_kills_nothing_bounded(self):
        """Review round 3, minor 1: a marked process can start another while
        the pass runs; the sweep repeats until a pass kills nothing, at most
        MARK_PASSES times."""
        passes = iter([[101], [102, 103], [], [104]])
        with mock.patch.object(opencode_http, "_marked_pids", lambda entry: next(passes)), \
                mock.patch.object(opencode_http.os, "kill") as kill:
            self.assertEqual(opencode_http.kill_marked("m"), [101, 102, 103])
        self.assertEqual([c.args[0] for c in kill.call_args_list], [101, 102, 103])
        with mock.patch.object(opencode_http, "_marked_pids", lambda entry: [7]), \
                mock.patch.object(opencode_http.os, "kill"):
            self.assertEqual(len(opencode_http.kill_marked("m")), opencode_http.MARK_PASSES)

    def test_the_pidfile_checks_the_boot_and_the_command_too(self):
        """Review round 2, minor 2: a pidfile from another boot, or naming a
        live process that is not `opencode serve`, kills nothing."""
        srv = self.server().start()
        self.killpg_later(srv.pid)
        pidfile = self.dir / "opencode.pid"
        opencode_http.write_pidfile(pidfile, srv.pid, marker=srv.marker)
        record = json.loads(pidfile.read_text())
        record["boot_id"] = "00000000-0000-0000-0000-000000000000"
        pidfile.write_text(json.dumps(record))
        self.assertEqual(opencode_http.reap_leftover(pidfile), [])
        self.assertFalse(_gone(srv.pid))
        other = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(other.kill)
        opencode_http.write_pidfile(pidfile, other.pid, marker="nobody")
        self.assertEqual(opencode_http.reap_leftover(pidfile), [])
        self.assertIsNone(other.poll())

    def test_a_recycled_pid_is_never_killed(self):
        """The pidfile names a start time: a live process with that pid but
        another start (the pid reused) is left alone."""
        srv = self.server().start()
        self.killpg_later(srv.pid)
        pidfile = self.dir / "opencode.pid"
        opencode_http.write_pidfile(pidfile, srv.pid)
        record = json.loads(pidfile.read_text())
        record["start"] = str(int(record["start"]) - 1)
        pidfile.write_text(json.dumps(record))
        self.assertEqual(opencode_http.reap_leftover(pidfile), [])
        self.assertFalse(_gone(srv.pid))
        self.assertFalse(pidfile.exists())
        for junk in ("", "{", "[]", '{"pid": "x"}', '{"pid": 1, "pgid": 1, "start": "0"}'):
            pidfile.write_text(junk)
            self.assertEqual(opencode_http.reap_leftover(pidfile), [], junk)
            self.assertFalse(pidfile.exists(), junk)


class FakeCase(HermeticCase):
    def fake(self, scripts=(), **kw):
        kw.setdefault("password", PASSWORD)
        fake = FakeOpencode(scripts, **kw).start()
        self.addCleanup(fake.close)
        return fake

    def client(self, fake, **kw):
        return OpencodeClient(fake.url, PASSWORD, **kw)


class TestOpencodeClient(FakeCase):
    def test_health_and_the_password(self):
        fake = self.fake()
        self.assertEqual(self.client(fake).health()["healthy"], True)
        with self.assertRaises(OpencodeError) as err:
            OpencodeClient(fake.url, "wrong").health()
        self.assertEqual(err.exception.status, 401)
        self.assertNotIn(PASSWORD, str(err.exception))
        self.assertTrue(fake.requests[0]["headers"]["authorization"].startswith("Basic "))

    def test_create_session(self):
        fake = self.fake()
        client = self.client(fake)
        session = client.create_session(title="wren")
        self.assertTrue(session["id"].startswith("ses_"))
        self.assertEqual(fake.requests[-1]["body"], {"title": "wren"})
        client.create_session()
        self.assertEqual(fake.requests[-1]["body"], {})

    def test_prompt_async_sends_the_v1_body(self):
        fake = self.fake()
        client = self.client(fake)
        sid = client.create_session()["id"]
        self.assertIsNone(client.prompt_async(sid, "hi", system="SYS", model="local/m1",
                                              agent="build"))
        request = fake.requests[-1]
        self.assertEqual((request["method"], request["path"]),
                         ("POST", "/session/%s/prompt_async" % sid))
        self.assertEqual(request["body"], {"parts": [{"type": "text", "text": "hi"}],
                                           "system": "SYS", "agent": "build",
                                           "model": {"providerID": "local", "modelID": "m1"}})
        fake.wait_event("session.idle")
        client.prompt_async(sid, "again", model={"providerID": "p", "modelID": "a/b"})
        self.assertEqual(fake.requests[-1]["body"], {
            "parts": [{"type": "text", "text": "again"}],
            "model": {"providerID": "p", "modelID": "a/b"}})
        with self.assertRaises(ValueError):
            client.prompt_async(sid, "x", model="no-slash")

    def test_errors_carry_the_status_and_a_body_excerpt(self):
        fake = self.fake()
        with self.assertRaises(OpencodeError) as err:
            self.client(fake).prompt_async("ses_nope", "hi")
        self.assertEqual(err.exception.status, 404)
        self.assertIn("Session not found: ses_nope", str(err.exception))
        self.assertIn("Session not found", err.exception.body)

    def test_abort_messages_and_permission_reply(self):
        fake = self.fake([[("ASK", "bash", {"command": "ls"}, "a b"), ("SLOW", 30)]])
        client = self.client(fake)
        sid = client.create_session()["id"]
        client.prompt_async(sid, "go")
        asked = fake.wait_event("permission.asked")["properties"]
        self.assertIs(client.reply_permission(asked["id"], "once"), True)
        self.assertEqual(fake.requests[-1]["body"], {"reply": "once"})
        fake.wait_event("message.updated", pred=lambda e: e["properties"]["info"].get(
            "finish") == "tool-calls")
        self.assertIs(client.abort(sid), True)
        fake.wait_event("session.idle")
        messages = client.messages(sid)
        self.assertEqual([m["info"]["role"] for m in messages], ["user", "assistant", "assistant"])
        self.assertEqual(messages[1]["parts"][1]["state"]["output"], "a b")
        self.assertEqual(messages[2]["info"]["error"]["name"], "MessageAbortedError")
        with self.assertRaises(OpencodeError) as err:
            client.reply_permission("per_nope", "reject")
        self.assertEqual(err.exception.status, 404)

    def test_config_and_mcp_status(self):
        fake = self.fake(config={"model": "local/m1"}, mcp={"cousin": {"status": "failed",
                                                                        "error": "x"}})
        client = self.client(fake)
        self.assertEqual(client.config(), {"model": "local/m1"})
        self.assertEqual(client.mcp_status(), {"cousin": {"status": "failed", "error": "x"}})

    def test_an_unreachable_server_is_an_error(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OpencodeError) as err:
            OpencodeClient("http://127.0.0.1:%d" % port, PASSWORD).health()
        self.assertIsNone(err.exception.status)

    def test_a_silent_server_times_out(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)                                      # accepts, never answers
        self.addCleanup(s.close)
        client = OpencodeClient("http://127.0.0.1:%d" % s.getsockname()[1], PASSWORD, timeout=0.5)
        t = time.monotonic()
        with self.assertRaises(OpencodeError):
            client.health()
        self.assertLess(time.monotonic() - t, 3.0)


class TestIterSse(HermeticCase):
    def test_frames_comments_and_multi_line_data(self):
        lines = ['data: {"type": "a"}', "", ": heartbeat", 'data: {"type":', 'data: "b"}', "",
                 "data: not json", "", "event: x", "id: 7", "retry: 5", 'data: {"type": "c"}',
                 "", "data: [1, 2]", "", 'data: {"type": "d"}']
        self.assertEqual(list(iter_sse(lines)), [{"type": "a"}, {"type": "b"}, {"type": "c"}])

    def test_bytes_and_crlf(self):
        self.assertEqual(list(iter_sse([b'data: {"type": "a"}\r\n', b"\r\n"])), [{"type": "a"}])


class TestEventReader(FakeCase):
    def reader(self, fake, on_event, password=PASSWORD, **kw):
        stop = threading.Event()
        kw.setdefault("backoff", 0.05)
        reader = EventReader(fake.url, password, on_event, stop_event=stop, **kw)
        reader.start()

        def cleanup():
            stop.set()
            reader.join(5)
        self.addCleanup(cleanup)
        return reader, stop

    def test_it_delivers_a_turns_events_after_server_connected(self):
        fake = self.fake([[("text", "Hello from fake.")]])
        got = []
        reader, _ = self.reader(fake, got.append)
        self.assertTrue(reader.connected.wait(5))
        client = self.client(fake)
        sid = client.create_session()["id"]
        client.prompt_async(sid, "hi")
        self.assertTrue(_wait(lambda: any(e["type"] == "session.idle" for e in got)))
        self.assertEqual(got[0]["type"], "server.connected")
        deltas = [e["properties"]["delta"] for e in got if e["type"] == "message.part.delta"]
        self.assertEqual("".join(deltas), "Hello from fake.")

    def test_it_reconnects_when_the_stream_drops(self):
        fake = self.fake()
        got = []
        reader, _ = self.reader(fake, got.append)
        self.assertTrue(fake.wait_subscribers(1))
        fake.drop_event_streams()
        self.assertTrue(_wait(lambda: [e["type"] for e in got].count("server.connected") == 2))
        self.assertEqual(reader.connects, 2)
        self.assertTrue(fake.wait_subscribers(1))
        self.client(fake).create_session()
        self.assertTrue(_wait(lambda: any(e["type"] == "session.created" for e in got)))

    def test_a_raising_callback_does_not_stop_it(self):
        fake = self.fake()
        got = []

        def on_event(event):
            got.append(event)
            raise RuntimeError("boom")
        reader, _ = self.reader(fake, on_event)
        self.assertTrue(fake.wait_subscribers(1))
        self.client(fake).create_session()
        self.assertTrue(_wait(lambda: any(e["type"] == "session.created" for e in got)))
        self.assertTrue(reader.is_alive())
        self.assertEqual(reader.connects, 1)

    def test_the_stop_event_ends_it_promptly_on_a_quiet_stream(self):
        fake = self.fake(heartbeat=30)
        reader, stop = self.reader(fake, lambda e: None)
        self.assertTrue(reader.connected.wait(5))
        t = time.monotonic()
        stop.set()
        reader.join(3)
        self.assertFalse(reader.is_alive())
        self.assertLess(time.monotonic() - t, 2.0)

    def test_a_refused_password_is_retried_never_raised(self):
        fake = self.fake()
        reader, stop = self.reader(fake, lambda e: None, password="wrong")
        self.assertTrue(_wait(lambda: sum(r["path"] == "/event" for r in fake.requests) >= 3))
        self.assertTrue(reader.is_alive())
        self.assertEqual(reader.connects, 0)
        self.assertIn("401", reader.last_error)
        stop.set()
        reader.join(3)
        self.assertFalse(reader.is_alive())


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_OPENCODE") == "1" and os.environ.get("OPENCODE_BIN"),
                     "live opencode: set COUSIN_LIVE_OPENCODE=1 and OPENCODE_BIN")
class TestLiveOpencodeServer(HermeticCase):
    """The real binary, in a throwaway HOME and XDG dirs, no credentials,
    no provider: health, a session, the event stream, stop."""

    def test_the_real_server_starts_serves_and_stops(self):
        tmp = Path(tempfile.mkdtemp(prefix="cousin-live-opencode-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        home = tmp / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp),
               "OPENCODE_DISABLE_MODELS_FETCH": "1"}          # no network: no models.dev
        for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                          ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
            env[name] = str(tmp / sub)
        config = tmp / "opencode.runner.json"
        config.write_text(json.dumps({"$schema": "https://opencode.ai/config.json",
                                      "disabled_providers": ["opencode"], "autoupdate": False,
                                      "share": "disabled"}))
        srv = OpencodeServer(os.environ["OPENCODE_BIN"], cwd=home, env=env, config_path=config,
                             timeout=60)
        self.addCleanup(srv.stop)
        srv.start()
        client = OpencodeClient(srv.url, srv.password)
        self.assertEqual(client.health()["healthy"], True)
        got, stop = [], threading.Event()
        reader = EventReader(srv.url, srv.password, got.append, stop_event=stop)
        reader.start()
        self.addCleanup(reader.join, 5)
        self.addCleanup(stop.set)
        self.assertTrue(_wait(lambda: bool(got), 15))
        self.assertEqual(got[0]["type"], "server.connected")
        session = client.create_session(title="wren")
        self.assertTrue(session["id"].startswith("ses_"))
        self.assertTrue(_wait(lambda: any(e["type"] == "session.created" for e in got), 15))
        self.assertEqual(client.abort(session["id"]), True)
        self.assertEqual(client.messages(session["id"]), [])
        with self.assertRaises(OpencodeError) as err:
            OpencodeClient(srv.url, "wrong").health()
        self.assertEqual(err.exception.status, 401)
        pid = srv.pid
        srv.stop()
        self.assertTrue(_gone(pid))
