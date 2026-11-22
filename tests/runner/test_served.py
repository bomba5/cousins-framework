"""served.served lists a real MCP server over stdio: a reworded tool and a
new parameter both move its digest (the CLI's own status shows neither)."""
import asyncio
import pathlib
import sys
import tempfile
import unittest

from cousin_lib.runner import served

SERVER = '''
import sys
from mcp.server.fastmcp import FastMCP
m = FastMCP("probe")
WORDS, EXTRA = sys.argv[1], sys.argv[2] == "1"
if EXTRA:
    @m.tool(description=WORDS)
    def drool(amount: float = 0.6, thick: float = 0.6) -> str:
        return "ok"
else:
    @m.tool(description=WORDS)
    def drool(amount: float = 0.6) -> str:
        return "ok"
m.run()
'''


class TestServedListing(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "probe.py").write_text(SERVER)

    def listed(self, words, extra):
        spec = {"type": "stdio", "command": sys.executable, "args": [str(self.dir / "probe.py"), words, extra]}
        return asyncio.run(served.served({"probe": spec, "gone": {"command": "/nonexistent/server"}}, self.dir))

    def test_wording_and_parameters_move_the_digest_and_a_broken_server_is_left_out(self):
        base = self.listed("drool from the lip", "0")
        self.assertEqual(sorted(base), ["probe"])                     # "gone" cannot start: left out
        self.assertEqual(base, self.listed("drool from the lip", "0"))   # stable
        self.assertNotEqual(base, self.listed("drool from the lower lip", "0"))   # reworded
        self.assertNotEqual(base, self.listed("drool from the lip", "1"))         # a new parameter


if __name__ == "__main__":
    unittest.main()
