"""One stream message bigger than the SDK's default buffer (a Read of a
large image) is parsed, not fatal to the turn."""
import asyncio
import json
import os
import unittest

from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

try:
    import claude_agent_sdk  # noqa: F401
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("claude-agent-sdk not installed")


class _Chunks:
    """The CLI's stdout as the transport reads it: text in 64 KiB chunks."""
    def __init__(self, text):
        self.parts = [text[i:i + 65536] for i in range(0, len(text), 65536)]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.parts:
            raise StopAsyncIteration
        return self.parts.pop(0)


def _read_one(options, line):
    async def run():
        t = SubprocessCLITransport(prompt="x", options=options)
        t._process, t._stdout_stream = object(), _Chunks(line)
        async for msg in t.read_messages():
            return msg
    return asyncio.run(run())


class TestStreamBuffer(HermeticCase):
    # a 2.08 MB PNG read back is ~2.8 MB of base64 in one message
    LINE = json.dumps({"type": "user", "image": "A" * 3_000_000}) + "\n"

    def test_a_three_megabyte_message_reaches_the_runner(self):
        from cousin_lib.runner.sdk import STREAM_BUFFER_BYTES, SdkRunner
        from tests.runner.test_sdk import ScriptedClient
        home = temp_home(self, runner="sdk"); (home.parent.parent / "config").mkdir(exist_ok=True)
        os.environ["FRAMEWORK_ROOT"] = str(home.parent.parent)
        r = SdkRunner(home, client_factory=lambda o: ScriptedClient(o, []))
        self.addCleanup(lambda: r.stop(timeout=5))
        opts = r.options()
        self.assertEqual(opts.max_buffer_size, STREAM_BUFFER_BYTES)
        self.assertEqual(len(_read_one(opts, self.LINE)["image"]), 3_000_000)

    def test_control_the_sdk_default_refuses_it(self):
        from claude_agent_sdk import ClaudeAgentOptions
        with self.assertRaisesRegex(Exception, "maximum buffer size"):
            _read_one(ClaudeAgentOptions(), self.LINE)


if __name__ == "__main__":
    unittest.main()
