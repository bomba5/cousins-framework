"""The structured handoff: one call, the ritual's order, STATUS kept outside its section."""
import importlib.util
import json
import unittest
from unittest import mock

from cousin_lib.runner import tools
from tests._hermetic import HermeticCase
from tests.runner.test_tools import _ctx

ARGS = {"position": "Halfway through the ledger audit.",
        "next_action": "Reconcile March against the bank export.",
        "status": "- ledger audit: March open\n- invoice run: waiting on Sam",
        "active_threads": ["ledger audit - March", "invoice run - blocked on Sam"],
        "learned": [{"topic": "bank export", "fact": "exports are UTC, not local"}]}


class TestHandoff(HermeticCase):
    def test_every_file_is_written(self):
        ctx = _ctx(self)
        text, err = tools.call(ctx, "handoff", dict(ARGS))
        self.assertFalse(err, text)
        home = ctx.home
        self.assertIn("March open", (home / "STATUS.md").read_text())
        self.assertIn("- ledger audit - March", (home / "data" / "active-threads.md").read_text())
        handoff = (home / "data" / "handoff.md").read_text()
        self.assertIn("Halfway through the ledger audit.", handoff)
        self.assertIn("Reconcile March", handoff)
        self.assertIn("degraded_state: false", handoff)
        raw = "".join(p.read_text() for p in (home / "memory" / "raw").glob("*.jsonl"))
        self.assertIn("exports are UTC", raw)

    def test_an_uncited_level_demoted_in_learned_is_said(self):
        ctx = _ctx(self)
        args = dict(ARGS, learned=[{"topic": "bank export", "fact": "exports are UTC",
                                    "level": "tool"}])
        text, err = tools.call(ctx, "handoff", args)
        self.assertFalse(err, text)
        self.assertIn("1 memory; memory 'bank export': demoted:", text)
        raw = "".join(p.read_text() for p in (ctx.home / "memory" / "raw").glob("*.jsonl"))
        self.assertIn("L3_COUSIN_CONCLUSION", raw)

    def test_a_cited_level_says_nothing_more(self):
        ctx = _ctx(self)
        args = dict(ARGS, learned=[{"topic": "bank export", "fact": "exports are UTC",
                                    "level": "tool", "cite": "export header, 2026-03"}])
        text, err = tools.call(ctx, "handoff", args)
        self.assertFalse(err, text)
        self.assertNotIn("demoted", text)

    def test_the_handoff_file_is_written_last(self):
        from cousin_lib import atomic
        ctx = _ctx(self); order = []
        real_write, real_atomic = type(ctx.home).write_text, atomic.write_text

        def spy(path, *a, **k):
            order.append(path.name); return real_write(path, *a, **k)

        def atomic_spy(path, *a, **k):
            order.append(path.name); return real_atomic(path, *a, **k)
        with mock.patch.object(type(ctx.home), "write_text", spy), \
                mock.patch.object(atomic, "write_text", atomic_spy):
            tools.call(ctx, "handoff", dict(ARGS))
        self.assertEqual(order[-1], "handoff.md")
        self.assertLess(order.index("STATUS.md"), order.index("active-threads.md"))

    def test_only_the_open_loops_section_of_status_is_replaced(self):
        ctx = _ctx(self)
        (ctx.home / "STATUS.md").write_text(
            "# Wren - STATUS\n\nOperator notes stay.\n\n## Open loops\n\n- old loop\n\n"
            "## Done\n\n- shipped the thing\n")
        tools.call(ctx, "handoff", dict(ARGS))
        text = (ctx.home / "STATUS.md").read_text()
        self.assertIn("Operator notes stay.", text)
        self.assertIn("## Done\n\n- shipped the thing", text)
        self.assertNotIn("old loop", text)
        self.assertIn("March open", text)

    def test_a_deeper_heading_holding_the_words_is_not_the_section(self):
        ctx = _ctx(self)
        (ctx.home / "STATUS.md").write_text(
            "# Wren - STATUS\n\n### Open loops archive\n\nold notes stay\n\n"
            "## Open loops\n\n- old loop\n")
        tools.call(ctx, "handoff", dict(ARGS))
        text = (ctx.home / "STATUS.md").read_text()
        self.assertIn("### Open loops archive\n\nold notes stay", text)
        self.assertNotIn("old loop", text)
        self.assertIn("March open", text)

    def test_the_words_in_prose_are_not_the_section(self):
        ctx = _ctx(self)
        (ctx.home / "STATUS.md").write_text(
            "# Wren - STATUS\n\nSee ## Open loops below.\n\n## Open loops\n\n- old loop\n")
        tools.call(ctx, "handoff", dict(ARGS))
        text = (ctx.home / "STATUS.md").read_text()
        self.assertIn("See ## Open loops below.", text)
        self.assertNotIn("old loop", text)

    def test_a_top_level_heading_after_the_section_is_kept(self):
        ctx = _ctx(self)
        (ctx.home / "STATUS.md").write_text(
            "# Wren - STATUS\n\n## Open loops\n\n- old loop\n\n# Appendix\n\nkept\n")
        tools.call(ctx, "handoff", dict(ARGS))
        text = (ctx.home / "STATUS.md").read_text()
        self.assertIn("# Appendix\n\nkept", text)
        self.assertNotIn("old loop", text)

    def test_a_crlf_heading_is_the_section_not_a_second_one(self):
        from cousin_lib.runner.tools import _with_open_loops
        crlf = ("# Wren - STATUS\r\n\r\nOperator notes stay.\r\n\r\n## Open loops\r\n\r\n"
                "- old loop\r\n\r\n## Done\r\n\r\n- shipped the thing\r\n")
        out = _with_open_loops(crlf, "Wren", "- new loop")
        self.assertEqual(out.count("## Open loops"), 1)
        self.assertNotIn("old loop", out)
        self.assertTrue(out.startswith("# Wren - STATUS\r\n\r\nOperator notes stay.\r\n\r\n"))
        self.assertTrue(out.endswith("## Done\r\n\r\n- shipped the thing\r\n"))
        self.assertIn("## Open loops\r\n\r\n- new loop\r\n", out)      # the file's own EOL

    def test_a_crlf_status_keeps_every_other_byte_through_the_tool(self):
        ctx = _ctx(self)
        path = ctx.home / "STATUS.md"
        head = b"# Wren - STATUS\r\n\r\nOperator notes stay.\r\n\r\n"
        tail = b"## Done\r\n\r\n- shipped the thing\r\n"
        path.write_bytes(head + b"## Open loops\r\n\r\n- old loop\r\n\r\n" + tail)
        text, err = tools.call(ctx, "handoff", dict(ARGS))
        self.assertFalse(err, text)
        data = path.read_bytes()
        self.assertEqual(data.count(b"## Open loops"), 1)
        self.assertNotIn(b"old loop", data)
        self.assertTrue(data.startswith(head))
        self.assertTrue(data.endswith(tail))
        self.assertIn(b"- ledger audit: March open\r\n- invoice run: waiting on Sam\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))                # no bare LF anywhere

    def test_a_status_without_the_section_gains_it_where_the_digest_reads_it(self):
        ctx = _ctx(self)
        (ctx.home / "STATUS.md").write_text("# Wren - STATUS\n\nfree text\n")
        tools.call(ctx, "handoff", dict(ARGS))
        from cousin_lib import boot
        self.assertIn("March open", boot._active_state(ctx.home))

    @unittest.skipUnless(hasattr(importlib.util.find_spec("cousin_lib.runner.prompt") and importlib.import_module("cousin_lib.runner.prompt"), "state_digest"),
                         "needs prompt.state_digest")
    def test_the_next_digest_reflects_the_handoff(self):
        import os
        from cousin_lib.runner import prompt
        ctx = _ctx(self)
        tools.call(ctx, "handoff", dict(ARGS))
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"}):
            text = prompt.state_digest(ctx.home, root=ctx.root, slug="wren")["text"]   # the passed root, never the env's
        self.assertIn("March open", text)
        self.assertIn("ledger audit - March", text)

    def test_missing_required_fields_are_a_tool_error(self):
        ctx = _ctx(self)
        text, err = tools.call(ctx, "handoff", {"position": "x"})
        self.assertTrue(err)
        self.assertIn("next_action", text)
        self.assertFalse((ctx.home / "data" / "handoff.md").exists())

    def test_a_wrong_shape_is_refused_before_any_write(self):
        bad = [("active_threads", "peer:toki - review"), ("active_threads", [1, 2]),
               ("learned", "bank export: exports are UTC"), ("learned", ["exports are UTC"]),
               ("learned", [{}]), ("learned", [{"topic": "bank export"}])]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                ctx = _ctx(self)
                (ctx.home / "STATUS.md").write_text("# Wren\n\n## Open loops\n\n- old loop\n")
                text, err = tools.call(ctx, "handoff", dict(ARGS, **{key: value}))
                self.assertTrue(err)
                self.assertIn(key, text)
                self.assertIn("old loop", (ctx.home / "STATUS.md").read_text())
                self.assertFalse((ctx.home / "data" / "active-threads.md").exists())
                self.assertFalse((ctx.home / "data" / "handoff.md").exists())

    def test_one_bad_memory_does_not_cost_the_handoff(self):
        ctx = _ctx(self)
        args = dict(ARGS, learned=[{"topic": "t", "fact": "f", "level": "operator"},   # no cite
                                   {"topic": "ok", "fact": "kept"}])
        text, err = tools.call(ctx, "handoff", args)
        self.assertFalse(err, text)
        self.assertIn("1 memory", text); self.assertIn("1 error", text)
        self.assertTrue((ctx.home / "data" / "handoff.md").exists())

    def test_on_handoff_receives_the_summary(self):
        seen = []
        ctx = _ctx(self); ctx.on_handoff = seen.append
        tools.call(ctx, "handoff", dict(ARGS))
        self.assertEqual(seen[0]["next_action"], ARGS["next_action"])
        self.assertEqual(seen[0]["learned"], 1)

    def test_the_schema_requires_the_three_fields(self):
        d = [t for t in tools.RUNNER_TOOLS if t["name"] == "handoff"][0]
        self.assertEqual(sorted(d["inputSchema"]["required"]), ["next_action", "position", "status"])


if __name__ == "__main__":
    unittest.main()
