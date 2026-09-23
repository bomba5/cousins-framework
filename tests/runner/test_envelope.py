"""The envelope: header, verbatim body, context marked as not the sender's."""
import base64
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

from cousin_lib.delivery import Item
from cousin_lib.runner import envelope
from tests._hermetic import HermeticCase

NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)


class TestRender(HermeticCase):
    def test_header_names_thread_source_sender_and_time(self):
        text = envelope.render(Item("operator:priya", "chat", "hi", sender="Priya"), now=NOW)
        self.assertTrue(text.startswith("[operator:priya] chat from Priya at 2026-09-23 08:00 UTC"), text)

    def test_a_multi_line_body_survives_intact(self):
        body = "line one\n\n    indented\nline three"
        text = envelope.render(Item("peer:testa", "chat", body, sender="Testa"), now=NOW)
        self.assertIn("\n\n" + body, text)

    def test_context_is_marked_and_never_inside_the_body_block(self):
        item = Item("operator:priya", "chat", "the body", sender="Priya",
                    context="[fw-recall] maybe relevant: notes/x.md")
        text = envelope.render(item, now=NOW)
        head, sep, tail = text.partition("--- context (not the sender's words) ---")
        self.assertTrue(sep)
        self.assertIn("the body", head)
        self.assertNotIn("fw-recall", head)
        self.assertIn("notes/x.md", tail)

    def test_no_context_means_no_context_block(self):
        text = envelope.render(Item("schedule", "schedule", "tick"), now=NOW)
        self.assertNotIn("context", text)

    def test_unknown_sender_is_named_as_such(self):
        text = envelope.render(Item("schedule", "schedule", "tick"), now=NOW)
        self.assertIn("from unknown at", text)


class TestRenderMessage(HermeticCase):
    def test_an_image_attachment_is_an_image_block_not_a_path(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        png = pathlib.Path(tmp.name) / "shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        item = Item("operator:priya", "chat", "look", sender="Priya", attachments=(str(png),))
        msg = envelope.render_message(item, now=NOW)
        self.assertEqual(msg["type"], "user")
        blocks = msg["message"]["content"]
        kinds = [b["type"] for b in blocks]
        self.assertEqual(kinds, ["text", "image"])
        img = blocks[1]["source"]
        self.assertEqual(img["media_type"], "image/png")
        self.assertEqual(base64.b64decode(img["data"]), b"\x89PNG\r\n\x1a\nfake")
        self.assertNotIn(str(png), blocks[0]["text"])

    def test_a_non_image_attachment_is_named_in_text(self):
        item = Item("operator:priya", "chat", "see", attachments=("/tmp/report.pdf",))
        msg = envelope.render_message(item, now=NOW)
        blocks = msg["message"]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "text"])
        self.assertIn("report.pdf", blocks[1]["text"])

    def test_an_image_suffix_without_a_file_is_an_attachment_not_an_image(self):
        item = Item("operator:priya", "chat", "see", attachments=("/tmp/missing.png",))
        text = envelope.render(item, now=NOW)
        self.assertIn("[attachment: missing.png]", text)
        self.assertNotIn("[image:", text)
        msg = envelope.render_message(item, now=NOW)
        blocks = msg["message"]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "text"])
        self.assertIn("missing.png", blocks[1]["text"])


if __name__ == "__main__":
    unittest.main()
