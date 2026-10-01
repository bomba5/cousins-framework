"""The fake `opencode serve` the default suite drives: its routes, its
basic auth, and the event shapes and orders measured on opencode 1.18.31
(including its queue and abort behaviour)."""
import base64
import http.client
import json
import threading
import time

from tests._hermetic import HermeticCase
from tests.runner._fake_opencode import FakeOpencode

PASSWORD = "wren-test-pw"


def call(fake, method, path, body=None, *, password=PASSWORD, raw=None):
    """One request; (status, headers, parsed body or text)."""
    conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
    headers = {}
    if password is not None:
        token = base64.b64encode(("opencode:%s" % password).encode()).decode()
        headers["Authorization"] = "Basic " + token
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode()
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = text
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed
    finally:
        conn.close()


def prompt(fake, sid, text, **extra):
    status, _, _ = call(fake, "POST", "/session/%s/prompt_async" % sid,
                        {"parts": [{"type": "text", "text": text}], **extra})
    return status


def kinds(events, sid=None):
    """The measured order, without the noise the runner ignores."""
    out = []
    for e in events:
        p = e["properties"]
        if sid is not None and p.get("sessionID", sid) != sid:
            continue
        if e["type"] == "message.updated":
            out.append("message.updated:%s" % p["info"]["role"])
        elif e["type"] == "message.part.updated":
            part = p["part"]
            label = part["type"]
            if label == "tool":
                label = "tool:%s" % part["state"]["status"]
            out.append("part:%s" % label)
        elif e["type"] == "session.status":
            out.append("status:%s" % p["status"]["type"])
        elif e["type"] == "session.error":
            out.append("error:%s" % p["error"]["name"])
        else:
            out.append(e["type"])
    return out


class _SSE:
    """A raw `GET /event` reader: every `data:` frame, parsed."""

    def __init__(self, case, fake):
        self.frames, self.lines = [], []
        self.conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=10)
        token = base64.b64encode(("opencode:%s" % PASSWORD).encode()).decode()
        self.conn.request("GET", "/event", headers={"Authorization": "Basic " + token})
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        self.headers = {k.lower(): v for k, v in self.resp.getheaders()}
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        case.addCleanup(self.close)

    def _read(self):
        try:
            for raw in self.resp:
                line = raw.decode().rstrip("\r\n")
                self.lines.append(line)
                if line.startswith("data: "):
                    self.frames.append(json.loads(line[6:]))
        except Exception:  # a dropped chunked stream can raise AttributeError in http.client
            pass

    def wait(self, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(pred(f) for f in list(self.frames)):
                return True
            time.sleep(0.02)
        return False

    def close(self):
        try:
            self.conn.sock and self.conn.sock.shutdown(2)
        except OSError:
            pass
        self.conn.close()
        self.thread.join(5)


class FakeCase(HermeticCase):
    def fake(self, scripts=(), **kw):
        kw.setdefault("password", PASSWORD)
        fake = FakeOpencode(scripts, **kw).start()
        self.addCleanup(fake.close)
        return fake

    def session(self, fake, title=None):
        status, _, body = call(fake, "POST", "/session", {"title": title} if title else {})
        self.assertEqual(status, 200)
        return body["id"]


class TestAuthAndHealth(FakeCase):
    def test_health_needs_the_password(self):
        fake = self.fake()
        status, headers, body = call(fake, "GET", "/global/health", password=None)
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("www-authenticate"), 'Basic realm="Secure Area"')
        self.assertIsNone(body)
        self.assertEqual(call(fake, "GET", "/global/health", password="wrong")[0], 401)
        status, _, body = call(fake, "GET", "/global/health")
        self.assertEqual((status, body), (200, {"healthy": True, "version": "1.18.31"}))

    def test_every_route_needs_the_password_and_every_request_is_recorded(self):
        fake = self.fake()
        for method, path in (("POST", "/session"), ("GET", "/event"), ("GET", "/config"),
                             ("GET", "/mcp"), ("POST", "/session/ses_x/abort")):
            with self.subTest(path=path):
                self.assertEqual(call(fake, method, path, password=None)[0], 401)
        self.assertEqual([(r["method"], r["path"], r["authorized"]) for r in fake.requests][:2],
                         [("POST", "/session", False), ("GET", "/event", False)])

    def test_config_and_mcp_are_what_it_was_given(self):
        config = {"model": "local/m1", "disabled_providers": ["opencode"]}
        fake = self.fake(config=config, mcp={"cousin": {"status": "connected"}})
        self.assertEqual(call(fake, "GET", "/config")[2], config)
        self.assertEqual(call(fake, "GET", "/mcp")[2], {"cousin": {"status": "connected"}})
        self.assertEqual(call(self.fake(), "GET", "/mcp")[2], {})

    def test_session_create_returns_a_session(self):
        fake = self.fake()
        status, _, body = call(fake, "POST", "/session", {"title": "wren"})
        self.assertEqual(status, 200)
        self.assertTrue(body["id"].startswith("ses_"))
        self.assertEqual(body["title"], "wren")
        self.assertEqual(fake.requests[-1]["body"], {"title": "wren"})
        created = fake.wait_event("session.created")
        self.assertEqual(created["properties"]["info"]["id"], body["id"])


class TestEventStream(FakeCase):
    def test_the_first_frame_is_server_connected_then_heartbeats(self):
        fake = self.fake(heartbeat=0.1)
        sse = _SSE(self, fake)
        self.assertEqual(sse.status, 200)
        self.assertEqual(sse.headers.get("content-type"), "text/event-stream")
        self.assertTrue(sse.wait(lambda f: f["type"] == "server.heartbeat"))
        self.assertEqual(sse.frames[0]["type"], "server.connected")
        self.assertTrue(sse.frames[0]["id"].startswith("evt_"))
        self.assertEqual(sse.lines[1], "")  # every frame is `data: {...}` then a blank line

    def test_a_turns_events_reach_every_subscriber(self):
        fake = self.fake([[("text", "Hello from fake.")]])
        one, two = _SSE(self, fake), _SSE(self, fake)
        self.assertTrue(fake.wait_subscribers(2))
        sid = self.session(fake)
        self.assertEqual(prompt(fake, sid, "hi"), 204)
        for sse in (one, two):
            self.assertTrue(sse.wait(lambda f: f["type"] == "session.idle"))
        deltas = [f["properties"]["delta"] for f in one.frames
                  if f["type"] == "message.part.delta"]
        self.assertEqual("".join(deltas), "Hello from fake.")

    def test_dropping_the_streams_closes_them(self):
        fake = self.fake()
        sse = _SSE(self, fake)
        self.assertTrue(fake.wait_subscribers(1))
        fake.drop_event_streams()
        sse.thread.join(5)
        self.assertFalse(sse.thread.is_alive())
        self.assertTrue(fake.wait_subscribers(0))


class TestScriptedTurn(FakeCase):
    def test_a_scripted_turns_event_order(self):
        script = [("reasoning", "pondering"), ("text", "Hello"),
                  ("tool", "bash", {"command": "ls"}, "a\nb\n"),
                  ("tool_error", "read", {"filePath": "/x"}, "denied by policy"),
                  ("text", "done")]
        fake = self.fake([script])
        sid = self.session(fake)
        self.assertEqual(prompt(fake, sid, "go", system="SYS"), 204)
        self.assertTrue(fake.settle())
        self.assertEqual(kinds(fake.events_since(0), sid)[1:], [
            "message.updated:user", "part:text", "status:busy",
            "message.updated:assistant", "part:step-start",
            "part:reasoning", "message.part.delta", "part:reasoning",
            "part:text", "message.part.delta", "part:text",
            "part:tool:pending", "part:tool:running", "part:tool:completed",
            "part:step-finish", "message.updated:assistant", "message.updated:assistant",
            "status:busy", "message.updated:assistant", "part:step-start",
            "part:tool:pending", "part:tool:running", "part:tool:error",
            "part:step-finish", "message.updated:assistant", "message.updated:assistant",
            "status:busy", "message.updated:assistant", "part:step-start",
            "part:text", "message.part.delta", "part:text",
            "part:step-finish", "message.updated:assistant", "message.updated:assistant",
            "status:busy", "status:idle", "session.idle", "message.updated:user"])

    def test_the_part_shapes_are_the_measured_ones(self):
        fake = self.fake([[("text", "Hello from fake."),
                           ("tool", "bash", {"command": "ls"}, "out\n"),
                           ("tool_error", "read", {"filePath": "/x"}, "nope")]])
        sid = self.session(fake)
        prompt(fake, sid, "go", system="SYS", agent="build",
               model={"providerID": "local", "modelID": "m1"})
        fake.wait_event("session.idle")
        events = fake.events_since(0)
        user = next(e for e in events if e["type"] == "message.updated")["properties"]["info"]
        self.assertEqual((user["role"], user["system"], user["model"], user["agent"]),
                         ("user", "SYS", {"providerID": "local", "modelID": "m1"}, "build"))
        parts = [e["properties"]["part"] for e in events if e["type"] == "message.part.updated"]
        text = [p for p in parts if p["type"] == "text" and p.get("time", {}).get("end")][0]
        self.assertEqual(text["text"], "Hello from fake.")
        self.assertTrue(text["id"].startswith("prt_") and text["messageID"].startswith("msg_"))
        self.assertEqual(text["sessionID"], sid)
        tool = [p for p in parts if p["type"] == "tool" and p["tool"] == "bash"]
        self.assertEqual(tool[0]["state"], {"status": "pending", "input": {}, "raw": ""})
        self.assertEqual(tool[1]["state"]["input"], {"command": "ls"})
        self.assertEqual(set(tool[1]["state"]["time"]), {"start"})
        self.assertEqual(tool[2]["state"]["output"], "out\n")
        self.assertEqual(set(tool[2]["state"]["time"]), {"start", "end"})
        self.assertEqual(len({p["callID"] for p in tool}), 1)
        self.assertTrue(tool[0]["callID"].startswith("call_"))
        err = [p for p in parts if p["type"] == "tool" and p["state"]["status"] == "error"][0]
        self.assertEqual((err["tool"], err["state"]["error"], err["state"]["input"]),
                         ("read", "nope", {"filePath": "/x"}))
        finishes = [p for p in parts if p["type"] == "step-finish"]
        self.assertEqual([p["reason"] for p in finishes], ["tool-calls", "tool-calls", "stop"])
        self.assertEqual(finishes[-1]["tokens"]["total"], 14)
        last = [e["properties"]["info"] for e in events if e["type"] == "message.updated"
                and e["properties"]["info"]["role"] == "assistant"][-1]
        self.assertEqual(last["finish"], "stop")
        self.assertIn("completed", last["time"])

    def test_the_messages_route_lists_the_turn(self):
        fake = self.fake([[("text", "Hello"), ("tool", "bash", {"command": "ls"}, "x")]])
        sid = self.session(fake)
        prompt(fake, sid, "go")
        fake.wait_event("session.idle")
        status, _, body = call(fake, "GET", "/session/%s/message" % sid)
        self.assertEqual(status, 200)
        self.assertEqual([m["info"]["role"] for m in body], ["user", "assistant", "assistant"])
        self.assertEqual(body[0]["parts"][0]["text"], "go")
        self.assertEqual([p["type"] for p in body[1]["parts"]],
                         ["step-start", "text", "tool", "step-finish"])
        self.assertEqual(body[1]["parts"][2]["state"]["status"], "completed")

    def test_scripts_run_in_order_and_then_a_default_text(self):
        fake = self.fake([[("text", "one")]])
        sid = self.session(fake)
        prompt(fake, sid, "a")
        fake.wait_event("session.idle")
        mark = len(fake.events)
        prompt(fake, sid, "b")
        fake.wait_event("session.idle", after=mark)
        texts = [e["properties"]["part"]["text"] for e in fake.events_since(0)
                 if e["type"] == "message.part.updated"
                 and e["properties"]["part"]["type"] == "text"
                 and e["properties"]["part"].get("time", {}).get("end")]
        self.assertEqual(texts, ["one", "ok"])

    def test_a_bad_prompt_is_refused_as_the_real_server_does(self):
        fake = self.fake()
        sid = self.session(fake)
        status, _, body = call(fake, "POST", "/session/%s/prompt_async" % sid,
                               {"parts": [{"type": "text", "text": "x"}], "model": "local/m1"})
        self.assertEqual((status, body["name"], body["data"]["kind"]), (400, "BadRequest", "Payload"))
        status, _, body = call(fake, "POST", "/session/ses_nope/prompt_async",
                               {"parts": [{"type": "text", "text": "x"}]})
        self.assertEqual((status, body), (404, {"name": "NotFoundError", "data": {
            "message": "Session not found: ses_nope"}}))
        status, _, body = call(fake, "POST", "/session/%s/prompt_async" % sid, raw=b"{nope")
        self.assertEqual((status, body["name"]), (500, "UnknownError"))
        self.assertEqual(call(fake, "GET", "/session/ses_nope/message")[0], 404)


class TestFailures(FakeCase):
    def test_auth_401_has_the_measured_shape(self):
        fake = self.fake([[("AUTH_401",)]])
        sid = self.session(fake)
        prompt(fake, sid, "hi")
        fake.wait_event("session.idle")
        self.assertTrue(fake.settle())
        events = fake.events_since(0)
        self.assertEqual(kinds(events, sid)[-7:], [
            "message.updated:assistant", "error:APIError", "status:idle", "session.idle",
            "message.updated:assistant", "status:idle", "session.idle"])
        error = [e for e in events if e["type"] == "session.error"][0]["properties"]["error"]
        self.assertEqual(error["data"]["statusCode"], 401)
        self.assertIs(error["data"]["isRetryable"], False)
        self.assertEqual(error["data"]["message"], "Incorrect API key provided")
        self.assertIn("invalid_api_key", error["data"]["responseBody"])
        info = [e for e in events if e["type"] == "message.updated"][-1]["properties"]["info"]
        self.assertEqual(info["error"], error)
        self.assertIn("completed", info["time"])

    def test_fail_names_the_error(self):
        fake = self.fake([[("text", "partial"), ("FAIL", "UnknownError", "Model not found: x/y")]])
        sid = self.session(fake)
        prompt(fake, sid, "hi")
        fake.wait_event("session.idle")
        error = fake.wait_event("session.error")["properties"]["error"]
        self.assertEqual(error, {"name": "UnknownError", "data": {"message": "Model not found: x/y"}})

    def test_abort_mid_slow_ends_the_turn_as_aborted(self):
        fake = self.fake([[("text", "part1 "), ("SLOW", 30), ("text", "never")]])
        sid = self.session(fake)
        prompt(fake, sid, "slow")
        fake.wait_event("message.part.delta")
        t = time.monotonic()
        status, _, body = call(fake, "POST", "/session/%s/abort" % sid)
        self.assertEqual((status, body), (200, True))
        fake.wait_event("session.idle")
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertTrue(fake.settle())
        events = fake.events_since(0)
        self.assertEqual(kinds(events, sid)[-7:], [
            "part:text", "error:MessageAbortedError", "status:idle", "session.idle",
            "message.updated:assistant", "status:idle", "session.idle"])
        error = [e for e in events if e["type"] == "session.error"][0]["properties"]["error"]
        self.assertEqual(error, {"name": "MessageAbortedError", "data": {"message": "Aborted"}})
        self.assertNotIn("never", json.dumps(events))
        self.assertEqual(fake.aborts, [sid])

    def test_hang_is_silent_until_abort(self):
        fake = self.fake([[("HANG",)]])
        sid = self.session(fake)
        prompt(fake, sid, "hang")
        fake.wait_event("session.status")
        time.sleep(0.3)
        self.assertNotIn("session.idle", [e["type"] for e in fake.events_since(0)])
        call(fake, "POST", "/session/%s/abort" % sid)
        self.assertEqual(fake.wait_event("session.error")["properties"]["error"]["name"],
                         "MessageAbortedError")

    def test_abort_while_idle_is_true_and_says_idle(self):
        fake = self.fake()
        sid = self.session(fake)
        mark = len(fake.events)
        self.assertEqual(call(fake, "POST", "/session/%s/abort" % sid)[2], True)
        self.assertEqual(call(fake, "POST", "/session/ses_nope/abort")[2], True)
        fake.wait_event("session.idle", after=mark)
        self.assertEqual(kinds(fake.events_since(mark), sid), ["status:idle", "session.idle"])


class TestQueue(FakeCase):
    def test_a_prompt_while_busy_is_answered_by_the_same_run(self):
        fake = self.fake([[("text", "first "), ("SLOW", 0.4), ("text", "done")],
                          [("text", "second")]])
        sid = self.session(fake)
        prompt(fake, sid, "one")
        fake.wait_event("message.part.delta")
        self.assertEqual(prompt(fake, sid, "two"), 204)
        # the queued user message is stored and announced at once
        user_two = fake.wait_event(
            "message.part.updated", pred=lambda e: e["properties"]["part"].get("text") == "two")
        self.assertIsNotNone(user_two)
        self.assertNotIn("session.idle", [e["type"] for e in fake.events_since(0)])
        self.assertTrue(fake.settle())
        events = fake.events_since(0)
        self.assertEqual([e["type"] for e in events].count("session.idle"), 1)
        assistants = [e["properties"]["info"] for e in events
                      if e["type"] == "message.updated"
                      and e["properties"]["info"]["role"] == "assistant"
                      and e["properties"]["info"].get("finish") == "stop"
                      and "completed" in e["properties"]["info"]["time"]]
        self.assertEqual(len(assistants), 2)
        body = call(fake, "GET", "/session/%s/message" % sid)[2]
        # listed in creation order: the queued prompt came after the first answer began
        self.assertEqual([m["info"]["role"] for m in body],
                         ["user", "assistant", "user", "assistant"])
        self.assertEqual(body[2]["parts"][0]["text"], "two")
        self.assertEqual(body[3]["parts"][1]["text"], "second")
        self.assertEqual(body[3]["info"]["parentID"], body[2]["info"]["id"])

    def test_abort_drops_a_queued_prompt(self):
        fake = self.fake([[("SLOW", 30)], [("text", "never")]])
        sid = self.session(fake)
        prompt(fake, sid, "one")
        fake.wait_event("session.status")
        prompt(fake, sid, "queued")
        fake.wait_event("message.part.updated",
                        pred=lambda e: e["properties"]["part"].get("text") == "queued")
        call(fake, "POST", "/session/%s/abort" % sid)
        fake.wait_event("session.idle")
        self.assertTrue(fake.settle())
        self.assertNotIn("never", json.dumps(fake.events_since(0)))
        body = call(fake, "GET", "/session/%s/message" % sid)[2]
        self.assertEqual([m["info"]["role"] for m in body], ["user", "assistant", "user"])
        # the next prompt starts a fresh run and takes the next script
        mark = len(fake.events)
        prompt(fake, sid, "three")
        fake.wait_event("session.idle", after=mark)
        self.assertIn("never", json.dumps(fake.events_since(mark)))


class TestPermission(FakeCase):
    def test_ask_waits_for_the_reply(self):
        fake = self.fake([[("ASK", "bash", {"command": "rm -rf x"}, "gone")],
                          [("ASK", "bash", {"command": "rm -rf y"}, "gone")]])
        sid = self.session(fake)
        prompt(fake, sid, "go")
        asked = fake.wait_event("permission.asked")["properties"]
        self.assertTrue(asked["id"].startswith("per_"))
        self.assertEqual((asked["sessionID"], asked["permission"], asked["patterns"]),
                         (sid, "bash", ["rm -rf x"]))
        self.assertEqual(call(fake, "POST", "/permission/%s/reply" % asked["id"],
                              {"reply": "maybe"})[0], 400)
        self.assertEqual(call(fake, "POST", "/permission/%s/reply" % asked["id"],
                              {"reply": "once"})[2], True)
        fake.wait_event("session.idle")
        replied = fake.wait_event("permission.replied")["properties"]
        self.assertEqual(replied, {"sessionID": sid, "requestID": asked["id"], "reply": "once"})
        done = fake.wait_event("message.part.updated", pred=lambda e: e["properties"]["part"]
                               .get("state", {}).get("status") == "completed")
        self.assertEqual(done["properties"]["part"]["state"]["output"], "gone")
        mark = len(fake.events)
        prompt(fake, sid, "again")
        asked = fake.wait_event("permission.asked", after=mark)["properties"]
        call(fake, "POST", "/permission/%s/reply" % asked["id"], {"reply": "reject"})
        err = fake.wait_event("message.part.updated", after=mark, pred=lambda e: e["properties"]
                              ["part"].get("state", {}).get("status") == "error")
        self.assertIn("rejected", err["properties"]["part"]["state"]["error"])
        status, _, body = call(fake, "POST", "/permission/per_nope/reply", {"reply": "reject"})
        self.assertEqual((status, body["_tag"]), (404, "PermissionNotFoundError"))
