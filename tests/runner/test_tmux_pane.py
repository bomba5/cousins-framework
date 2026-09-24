"""The tmux kind's pane (phase 11 Task 2, interfaces I3): how the runner
drives an interactive Claude Code in a tmux session on the framework's own
socket. A fake tmux logs every argv and serves a scripted screen; the
screens are the ones captured from Claude Code 2.1.281 (phase 11
findings: S0, S3, S4, I6a, Z7, Z8), so the parsing is held to what the CLI
really shows."""
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

from cousin_lib.runner import tmux_pane as tp

RULE = "─" * 60
IDLE = "\n".join([
    "● kestrel-violet",
    "✻ Churned for 2s · done 17.09",
    RULE, "❯ ", RULE,
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"])
TEXT_IN_BOX = IDLE.replace("❯ \n", "❯ stranded paste text\n")
COLLAPSED = IDLE.replace("❯ \n", "❯ [Pasted text #1 +3 lines]\n")
QUEUED = "\n".join([
    "  two hundred and forty-nine",
    "❯ [inbox:4715] Change of plan: stop the list where you are and write only the word HERON.",
    "  ctrl+x ctrl+s to send now",
    RULE, "❯ Press up to edit queued messages", RULE,
    "  -- INSERT -- ⏸ manual mode on · ← for agents"])
TRUST = "\n".join([
    " Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known",
    " ❯ No, exit", "   Yes, I trust this folder", " Enter to confirm · Esc to cancel"])
LOGIN = "\n".join([
    " Claude Code can be used with your Claude subscription or billed based on API usage through your Console account.",
    " Select login method:",
    " ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise",
    "   2. Anthropic Console account · API usage billing"])
REWIND = "\n".join([
    "    [inbox:4721] Reply with the word OK only.", "    No code changes",
    "  ❯ (current)", "  Enter to continue · Esc to cancel"])

FAKE_TMUX = """#!%s
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_TMUX_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\\n")
fail = os.environ.get("FAKE_TMUX_FAIL", "")
sub = next((a for a in args if a in ("has-session", "display-message", "capture-pane", "new-session",
            "kill-session", "send-keys", "load-buffer", "paste-buffer", "set-option", "resize-window")), "")
if sub and sub == fail:
    sys.exit(1)
target = args[args.index("-t") + 1] if "-t" in args else ""
PANE_CMDS = ("display-message", "capture-pane", "send-keys", "paste-buffer", "set-option", "resize-window")
if sub in PANE_CMDS and target.startswith("=") and not target.endswith(":"):
    sys.exit(1)       # tmux 3.6a: `=name` is no pane target (measured); `=name:` is
if sub in ("has-session", "kill-session") and not (target.startswith("=") and ":" not in target):
    sys.exit(1)       # sessions by exact name only
if sub == "has-session":
    sys.exit(int(os.environ.get("FAKE_TMUX_HAS", "0")))
if sub == "display-message":
    print(os.environ.get("FAKE_TMUX_PID", "4242"))
if sub == "capture-pane":
    sys.stdout.write(open(os.environ["FAKE_TMUX_SCREEN"]).read())
if sub == "load-buffer":
    open(os.environ["FAKE_TMUX_LOG"] + ".buffer", "w").write(sys.stdin.read())
sys.exit(0)
""" % sys.executable


class PaneCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.tmux = self.dir / "tmux"
        self.tmux.write_text(FAKE_TMUX)
        self.tmux.chmod(0o755)
        self.log = self.dir / "log"
        self.screen = self.dir / "screen"
        self.screen.write_text(IDLE)
        self.env = {"FAKE_TMUX_LOG": str(self.log), "FAKE_TMUX_SCREEN": str(self.screen)}
        old = {k: os.environ.get(k) for k in list(self.env) + ["FAKE_TMUX_FAIL", "FAKE_TMUX_HAS"]}
        os.environ.update(self.env)
        os.environ.pop("FAKE_TMUX_FAIL", None)

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.sock = self.dir / "run" / "tmux.sock"
        self.pane = tp.TmuxPane(self.sock, "tmux-wren", tmux_bin=str(self.tmux))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def subs(self):
        return [next(a for a in c if not a.startswith("-") and a not in (str(self.sock),)) for c in self.calls()]


class TestStart(PaneCase):
    def test_the_socket_dir_is_private_and_the_env_is_set_inside_the_pane(self):
        self.pane.start(["/abs/python3", "/abs/tmux_launch.py", "--home", "/h/wren", "--", "claude"],
                        cwd="/h/wren", env_base={"HOME": "/h", "PATH": "/bin", "LANG": "C.UTF-8"})
        self.assertEqual(stat.S_IMODE(self.sock.parent.stat().st_mode), 0o700)
        new = next(c for c in self.calls() if "new-session" in c)
        self.assertEqual(new[:2], ["-S", str(self.sock)])
        self.assertIn("-c", new)
        self.assertEqual(new[new.index("-c") + 1], "/h/wren")
        command = new[-1]
        self.assertTrue(command.startswith("exec env -i "), command)
        self.assertIn("HOME=/h", command)
        self.assertIn("/abs/tmux_launch.py", command)
        self.assertTrue(any("window-size" in c and "manual" in c for c in self.calls()))

    def test_alive_pid_kill(self):
        self.assertTrue(self.pane.alive())
        self.assertEqual(self.pane.pid(), 4242)
        self.pane.kill()
        self.assertIn("kill-session", self.subs())
        os.environ["FAKE_TMUX_HAS"] = "1"
        self.assertFalse(self.pane.alive())


class TestScreen(PaneCase):
    def show(self, text):
        self.screen.write_text(text)

    def test_the_box(self):
        self.assertEqual(self.pane.box_text(), "")
        self.show(TEXT_IN_BOX)
        self.assertEqual(self.pane.box_text(), "stranded paste text")
        self.show(COLLAPSED)
        self.assertEqual(self.pane.box_text(), "[Pasted text #1 +3 lines]")
        self.show("no box here at all")
        self.assertIsNone(self.pane.box_text())

    def test_queued_input_is_not_an_empty_box(self):
        self.show(QUEUED)
        self.assertTrue(self.pane.queued())
        self.show(IDLE)
        self.assertFalse(self.pane.queued())

    def test_attention_screens(self):
        self.assertIsNone(self.pane.attention())
        for text, want in ((TRUST, "trust"), (LOGIN, "login"), (REWIND, "rewind"),
                           ("Claude usage limit reached. Your limit resets at 5pm", "limit")):
            self.show(text)
            self.assertEqual(self.pane.attention(), want, want)

    def test_enter_to_continue_alone_is_not_the_rewind_selector(self):
        self.show("  Enter to continue")
        self.assertIsNone(self.pane.attention())


class TestTyping(PaneCase):
    def test_a_row_is_typed_then_pasted_bracketed_then_entered(self):
        out = self.pane.type_row("[inbox:0123456789ab] (Chat Wren) read this", "line one\nline two")
        self.assertEqual(out, tp.Outcome.TYPED)
        keys = [c for c in self.calls() if "send-keys" in c or "paste-buffer" in c or "load-buffer" in c]
        self.assertIn("-l", keys[0])
        self.assertEqual(keys[0][-1], "[inbox:0123456789ab] (Chat Wren) read this")
        self.assertIn("load-buffer", keys[1])
        paste = keys[2]
        self.assertIn("paste-buffer", paste)
        self.assertIn("-p", paste)
        self.assertIn("-d", paste)
        self.assertEqual(keys[3][-1], "Enter")
        self.assertEqual((self.log.parent / "log.buffer").read_text(), "\n\nline one\nline two")

    def test_no_body_is_no_paste(self):
        self.assertEqual(self.pane.type_row("[inbox:0123456789ab] hi", ""), tp.Outcome.TYPED)
        self.assertFalse(any("paste-buffer" in c for c in self.calls()))

    def test_an_attention_screen_is_never_typed_into(self):
        for text in (TRUST, LOGIN, REWIND):
            self.screen.write_text(text)
            self.log.write_text("")
            self.assertEqual(self.pane.type_row("[inbox:0123456789ab] x", "y"), tp.Outcome.BLOCKED)
            self.assertFalse(any("send-keys" in c or "paste-buffer" in c for c in self.calls()))

    def test_a_box_with_text_or_queued_input_blocks(self):
        for text in (TEXT_IN_BOX, QUEUED):
            self.screen.write_text(text)
            self.log.write_text("")
            self.assertEqual(self.pane.type_row("[inbox:0123456789ab] x", ""), tp.Outcome.BLOCKED)
            self.assertFalse(any("send-keys" in c for c in self.calls()))

    def test_a_tmux_failure_is_failed(self):
        os.environ["FAKE_TMUX_FAIL"] = "send-keys"
        self.assertEqual(self.pane.type_row("[inbox:0123456789ab] x", ""), tp.Outcome.FAILED)

    def test_keys_are_an_allowlist_and_clear_is_ctrl_u(self):
        self.pane.clear()
        self.assertEqual(self.calls()[-1][-1], "C-u")
        with self.assertRaises(ValueError):
            self.pane.key("y")


if __name__ == "__main__":
    unittest.main()
