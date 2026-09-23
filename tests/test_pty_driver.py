"""The pty driver against a scripted CLI stand-in: real pty, scripted peer."""
import pathlib
import sys
import tempfile
import textwrap
import time
import unittest

from cousin_lib import pty_driver
from tests._hermetic import HermeticCase

URL = "https://claude.com/cai/oauth/authorize?code=true&x=1"
URL_RX = r"(https://claude\.com/cai/oauth/authorize\?\S+)\s"

FAKE_CLI = textwrap.dedent(r'''
    import shutil, sys
    sys.stdout.write("columns=%d\r\n" % shutil.get_terminal_size().columns)
    url = "https://claude.com/cai/oauth/authorize?code=true&x=1"
    sys.stdout.write("Opening browser to sign in...\r\n")
    sys.stdout.write("If the browser didn't open, visit: \x1b]8;;%s\x07%s\x1b]8;;\x07\r\n"
                     % (url, url))
    for i in range(300):                    # a redrawn status line, as setup-token draws
        sys.stdout.write("\x1b[2K\x1b[1G\x1b[1mWorking %d\x1b[0m" % i)
    sys.stdout.write("\r\nPaste\x1b[1Ccode\x1b[1Chere\x1b[1Cif\x1b[1Cprompted\x1b[1C>\x1b[1C")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    sys.stdout.write("\r\nLogin successful.\r\n" if code == "good-code" else
                     "\r\nInvalid code. Please make sure the full code was copied\r\n")
    sys.stdout.flush()
''')

FLOOD = "import sys\nsys.stdout.write('x' * 2000000 + '\\nEND\\n')\nsys.stdout.flush()\n"


class TestClean(HermeticCase):
    def test_cursor_forward_becomes_spaces_and_escapes_go(self):
        raw = "\x1b[1mPaste\x1b[1Ccode\x1b[2Chere\x1b[0m\r\n\x1b]0;title\x07done"
        self.assertEqual(pty_driver.clean(raw), "Paste code  here\ndone")

    def test_an_osc_8_hyperlink_leaves_the_url_once(self):
        for st in ("\x07", "\x1b\\"):               # BEL and ST both end an OSC
            raw = "visit: \x1b]8;;%s%s%s\x1b]8;;%s\r\n" % (URL, st, URL, st)
            self.assertEqual(pty_driver.clean(raw), "visit: %s\n" % URL)

    def test_an_escape_cut_by_a_read_waits_for_its_end(self):
        c = pty_driver.Cleaner()
        out = (c.feed("Paste\x1b[") + c.feed("1Ccode\x1b]8;;https://x")
               + c.feed("\x07link\x1b]8;;\x07"))
        self.assertEqual(out, "Paste codelink")


class TestPtySession(HermeticCase):
    def spawn(self, source):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        script = pathlib.Path(tmp.name) / "fake_cli.py"; script.write_text(source)
        s = pty_driver.PtySession([sys.executable, str(script)], {"PATH": "/usr/bin:/bin"})
        self.addCleanup(s.close)
        return s

    def test_the_url_the_prompt_and_the_answer(self):
        s = self.spawn(FAKE_CLI)
        s.read_until(r"columns=2000\b", 10)          # the size was set in the child, before exec
        self.assertEqual(s.read_until(URL_RX, 10).group(1), URL)
        s.read_until(r"Paste\s*code\s*here\s*if\s*prompted\s*>", 10)
        s.write("good-code\r")
        self.assertEqual(s.read_until(r"(Login successful|Invalid code)", 10).group(1),
                         "Login successful")

    def test_a_match_is_consumed(self):
        s = self.spawn(FAKE_CLI)
        s.read_until(r"visit:", 10)
        with self.assertRaises(pty_driver.PtyTimeout):
            s.read_until(r"Opening browser", 0.3)    # before the previous match: not found again

    def test_the_reader_drains_while_nobody_reads(self):
        s = self.spawn(FLOOD)
        deadline = time.monotonic() + 10
        while "END" not in s.text() and time.monotonic() < deadline:
            time.sleep(0.05)                          # no read_until: only the reader thread reads
        self.assertIn("END", s.text())
        self.assertLessEqual(len(s.text()), pty_driver.TAIL_CHARS)

    def test_a_stall_is_a_timeout_not_a_hang(self):
        s = self.spawn(FAKE_CLI)
        with self.assertRaises(pty_driver.PtyTimeout):
            s.read_until(r"this never appears", 0.5)

    def test_a_program_name_without_a_path_is_refused(self):
        with self.assertRaises(ValueError):
            pty_driver.PtySession(["python3", "-c", "pass"], {"PATH": "/usr/bin:/bin"})


if __name__ == "__main__":
    unittest.main()
