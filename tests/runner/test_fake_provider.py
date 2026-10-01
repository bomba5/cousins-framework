"""The fake OpenAI-compatible provider (tests/runner/_fake_provider.py),
the live tests' only model endpoint: its scripted replies in the streaming
shape opencode's bundled `@ai-sdk/openai-compatible` reads, and what it
records. Loopback only; never the opencode binary."""
import http.client
import json
import time
import unittest

from tests._hermetic import HermeticCase
from tests.runner._fake_provider import TITLE, FakeProvider

TOOLS = [{"type": "function", "function": {"name": "cousin_reply", "parameters": {}}},
         {"type": "function", "function": {"name": "cousin_handoff", "parameters": {}}}]


def _chat(provider, *, tools=TOOLS, stream=True, messages=None, timeout=10):
    body = {"model": "m1", "stream": stream, "tools": tools, "messages": messages or [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]}
    conn = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=timeout)
    conn.request("POST", "/v1/chat/completions", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp, conn


def _frames(resp):
    out = []
    for line in resp.read().decode().splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


class TestFakeProvider(HermeticCase):
    def provider(self, script=()):
        p = FakeProvider(script).start()
        self.addCleanup(p.close)
        return p

    def test_text_is_streamed_as_chunks_then_done(self):
        p = self.provider([("text", "Hello from the fake.")])
        resp, conn = _chat(p)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        frames = _frames(resp)
        conn.close()
        text = "".join(c["delta"].get("content") or "" for f in frames for c in f["choices"])
        self.assertEqual(text, "Hello from the fake.")
        self.assertEqual([c["finish_reason"] for f in frames for c in f["choices"]
                          if c["finish_reason"]], ["stop"])
        self.assertEqual(frames[-1]["usage"]["total_tokens"], 14)
        self.assertEqual(p.chats()[0]["reply"], ["text", "Hello from the fake."])
        self.assertTrue(p.chats()[0]["completed"])

    def test_a_tool_call_names_the_tool_and_its_arguments(self):
        p = self.provider([("tool", "cousin_reply", {"text": "hi"}), ("text", "done")])
        resp, conn = _chat(p)
        frames = _frames(resp)
        conn.close()
        call = frames[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "cousin_reply")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"text": "hi"})
        self.assertTrue(call["id"])
        self.assertEqual(frames[1]["choices"][0]["finish_reason"], "tool_calls")
        # the next request carries the tool's result back: recorded
        resp, conn = _chat(p, messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "tool_calls": [{"id": call["id"], "type": "function",
                                                  "function": call["function"]}]},
            {"role": "tool", "tool_call_id": call["id"], "content": "replied to priya (#1)"}])
        _frames(resp)
        conn.close()
        second = p.chats()[1]
        self.assertEqual(second["tool_results"], [{"tool_call_id": call["id"],
                                                   "content": "replied to priya (#1)"}])
        self.assertEqual(second["roles"], ["user", "assistant", "tool"])
        self.assertEqual(p.chats()[0]["tools"], ["cousin_reply", "cousin_handoff"])
        self.assertEqual(p.chats()[0]["last_user"], "hello")

    def test_a_request_without_tools_is_answered_a_title_and_consumes_nothing(self):
        p = self.provider([("text", "the agent's answer")])
        resp, conn = _chat(p, tools=[])
        frames = _frames(resp)
        conn.close()
        self.assertEqual("".join(c["delta"].get("content") or "" for f in frames
                                 for c in f["choices"]), TITLE)
        resp, conn = _chat(p)
        frames = _frames(resp)
        conn.close()
        self.assertEqual("".join(c["delta"].get("content") or "" for f in frames
                                 for c in f["choices"]), "the agent's answer")
        self.assertEqual([r["aux"] for r in p.chats(aux=True)], [True, False])
        self.assertEqual(len(p.chats()), 1)

    def test_a_401_is_openais_invalid_key_error(self):
        p = self.provider([("status", 401, "Incorrect API key provided")])
        resp, conn = _chat(p)
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 401)
        self.assertEqual(body["error"], {"message": "Incorrect API key provided",
                                         "type": "invalid_request_error",
                                         "code": "invalid_api_key"})

    def test_a_slow_reply_sends_its_first_word_then_holds_until_closed(self):
        p = self.provider([("slow", 30, "first then the rest")])
        resp, conn = _chat(p, timeout=5)
        first = resp.fp.readline()
        self.assertTrue(first.startswith(b"data: "))
        self.assertEqual(json.loads(first[6:])["choices"][0]["delta"]["content"], "first ")
        t0 = time.monotonic()
        p.close()
        resp.read()                 # the stream ends at close, not after 30 s
        conn.close()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertFalse(p.chats()[0].get("completed"))

    def test_the_whole_reply_when_not_streaming_and_404_elsewhere(self):
        p = self.provider([("text", "whole")])
        resp, conn = _chat(p, stream=False)
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(body["choices"][0]["message"]["content"], "whole")
        conn = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
        conn.request("GET", "/v1/models")
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()
        self.assertEqual([(r["method"], r["path"], r["chat"]) for r in p.requests],
                         [("POST", "/v1/chat/completions", True), ("GET", "/v1/models", False)])

    def test_an_unknown_reply_is_refused(self):
        with self.assertRaises(ValueError):
            FakeProvider([("sing", "la")])


if __name__ == "__main__":
    unittest.main()
