"""The chat composer sends on a phone keyboard's Enter, pinned by text
(the page compiles its JSX in the browser and has no build step).

The failure it holds off: on Android the keyboard's Enter reaches the
textarea as an input event (keyCode 229), not as an "Enter" keydown, so
onKey never saw it and the message got a new line instead of being sent.
"""
import pathlib
import unittest

_CHAT = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static" / "chat.jsx"


class ComposerEnter(unittest.TestCase):

    def setUp(self):
        self.src = _CHAT.read_text(encoding="utf-8")

    def test_the_keyboard_shows_a_send_key(self):
        self.assertIn('enterKeyHint="send"', self.src)

    def test_a_line_break_input_sends(self):
        self.assertIn('addEventListener("beforeinput"', self.src)
        self.assertIn('"insertLineBreak"', self.src)
        self.assertIn("sendRef.current()", self.src)

    def test_shift_enter_still_makes_a_line(self):
        self.assertIn('shiftEnterRef.current = e.key === "Enter" && e.shiftKey', self.src)


if __name__ == "__main__":
    unittest.main()
