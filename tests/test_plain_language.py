"""The plain-language check (docs/plain-language.md): the rules, the
Markdown it skips, the two glossary formats, the config, the CLI's exit
codes and fixes, and the reply modes of the reply and send tools."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import mcp_server
from cousin_lib import plain_language as pl
from cousin_lib.runner import tools
from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase

DASH = "\u2014"
EN = "\u2013"


def rules(found):
    return [f.rule for f in found]


def words(n, first="the"):
    return " ".join([first] + ["word"] * (n - 1))


class TestRules(unittest.TestCase):
    def test_an_em_dash_is_found_with_its_fix_and_an_en_dash_only_between_spaces(self):
        found = pl.check("one %s two, one%sthree, 1%s9, a %s b" % (DASH, DASH, EN, EN))
        self.assertEqual(rules(found), ["em-dash", "em-dash", "em-dash"])
        self.assertEqual([(f.line, f.col, f.fix) for f in found],
                         [(1, 4, " - "), (1, 15, " - "), (1, 29, " - ")])
        self.assertEqual(pl.apply_fixes("one %s two, one%sthree, 1%s9, a %s b"
                                        % (DASH, DASH, EN, EN), found),
                         "one - two, one - three, 1%s9, a - b" % EN)

    def test_code_urls_and_fences_are_not_checked(self):
        text = ("Run `a %s b` and see https://example.com/x%sy please.\n\n"
                "```\nleverage %s here\n```\n" % (DASH, DASH, DASH))
        cfg = pl.default_config()
        cfg["banned"] = [{"word": "leverage", "use": "use"}]
        self.assertEqual(pl.check(text, config=cfg), [])
        self.assertEqual(rules(pl.check(text.replace("`a", "a").replace("b`", "b"))),
                         ["em-dash"])

    def test_table_cells_are_text_and_the_separator_row_is_skipped(self):
        text = "| a | b |\n|---|---|\n| fine | one %s two |\n" % DASH
        found = pl.check(text)
        self.assertEqual([(f.rule, f.line, f.col) for f in found], [("em-dash", 3, 13)])
        # no rule can fire on a row of dashes, so the skip is pinned on the cells read
        self.assertEqual([(b.kind, b.segments[0][0]) for b in pl._blocks(text)],
                         [("cell", 0), ("cell", 0), ("cell", 2), ("cell", 2)])
        long_cell = "| x | %s. |\n|---|---|\n" % words(26)
        self.assertEqual(rules(pl.check("| h | h |\n|---|---|\n" + long_cell.split("\n")[0])),
                         ["sentence-length"])

    def test_sentence_limits_step_and_description(self):
        self.assertEqual(pl.check(words(25) + "."), [])
        self.assertEqual(rules(pl.check(words(26) + ".")), ["sentence-length"])
        self.assertIn("26 words", pl.check(words(26) + ".")[0].message)
        # a numbered item, or a bullet that starts with a verb, is a step: 20
        self.assertEqual(rules(pl.check("1. " + words(21) + ".")), ["sentence-length"])
        self.assertEqual(rules(pl.check("- " + words(21, "run") + ".")), ["sentence-length"])
        self.assertEqual(pl.check("- " + words(21, "the") + "."), [])
        self.assertEqual(pl.check("1. " + words(20) + "."), [])
        # a heading is not a sentence; two sentences are counted apart
        self.assertEqual(pl.check("# " + words(40)), [])
        self.assertEqual(pl.check(words(20) + ". " + words(20) + "."), [])
        # wrapped lines are one sentence, reported where it starts
        found = pl.check("Intro.\n" + words(13) + "\n" + words(13) + ".")
        self.assertEqual([(f.line, f.col) for f in found], [(2, 1)])

    def test_markup_is_not_counted_and_a_code_span_is_one_word(self):
        text = "**Bold** [link text](https://example.com/very/long/path) and `a b c d e f`"
        self.assertEqual(pl.check(text + " " + words(20) + "."), [])
        self.assertEqual(rules(pl.check(text + " " + words(21) + ".")), ["sentence-length"])

    def test_limits_and_rules_come_from_the_config(self):
        cfg = pl.default_config()
        cfg["limits"]["description"] = 5
        self.assertEqual(rules(pl.check(words(6) + ".", config=cfg)), ["sentence-length"])
        cfg["rules"]["sentence_length"] = False
        self.assertEqual(pl.check(words(6) + ".", config=cfg), [])

    def test_banned_words_match_whole_words_in_any_case_across_a_line_wrap(self):
        cfg = pl.default_config()
        cfg["banned"] = [{"word": "leverage", "use": "use"}, {"word": "going forward", "use": ""}]
        found = pl.check("We Leverage it.\nNo leverages. Going\nforward.", config=cfg)
        self.assertEqual([(f.rule, f.line, f.excerpt) for f in found],
                         [("banned-word", 1, "Leverage"), ("banned-word", 2, "Going forward")])
        self.assertIn('use "use"', found[0].message)

    def test_passive_is_a_note(self):
        found = pl.check("It was written by us. It is often used. It is open.")
        self.assertEqual([(f.rule, f.severity, f.excerpt) for f in found],
                         [("passive", "note", "was written"), ("passive", "note", "is often used")])

    def test_glossary_words_name_the_preferred_term(self):
        glossary = {"chat thread": {"meaning": "", "avoid": ["thread", "conversation"]}}
        found = pl.check("A chat thread, a thread, a Conversation.", glossary=glossary)
        self.assertEqual([f.excerpt for f in found], ["thread", "Conversation"])
        self.assertIn('says "chat thread"', found[0].message)


class TestGlossary(unittest.TestCase):
    def test_the_table_format(self):
        text = ("Intro.\n\n| Term | Uses | MEANING | Do not use |\n|:--|--|--|--|\n"
                "| `reply` | 3 | an answer \\| short | response, answer message |\n"
                "| send | 1 | to a peer | - |\n")
        self.assertEqual(pl.parse_glossary(text), {
            "reply": {"meaning": "an answer | short", "avoid": ["response", "answer message"]},
            "send": {"meaning": "to a peer", "avoid": []}})

    def test_the_heading_format_and_the_table_winning(self):
        text = ("# Glossary\n\n### reply\n\nan answer\non the surface.\nDo not use: response, ack\n\n"
                "### flip\n\na new session.\n\n| term | meaning | do not use |\n|---|---|---|\n"
                "| flip | from the table | restart |\n")
        self.assertEqual(pl.parse_glossary(text), {
            "reply": {"meaning": "an answer on the surface.", "avoid": ["response", "ack"]},
            "flip": {"meaning": "from the table", "avoid": ["restart"]}})

    def test_the_framework_glossary_reads(self):
        doc = pathlib.Path(__file__).resolve().parents[1] / "docs" / "glossary.md"
        entries = pl.load_glossary(doc)
        self.assertIn("cousin", entries)
        self.assertTrue(entries["cousin"]["meaning"].startswith("a persistent agent"))


class TestConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()

    def test_missing_is_the_defaults_and_a_write_reads_back(self):
        self.assertEqual(pl.load_config(self.root), pl.default_config())
        cfg = pl.default_config()
        cfg.update(reply_default="note", glossary="docs/g.md",
                   banned=[{"word": 'say "x"', "use": "y"}])
        cfg["rules"]["passive"] = False
        pl.write_config(self.root, cfg)
        self.assertEqual(pl.load_config(self.root), cfg)

    def test_bad_values_are_refused_by_name(self):
        for bad, word in (({"limits": {"step": 0}}, "limits.step"),
                          ({"limits": {"step": True}}, "limits.step"),
                          ({"rules": {"em-dash": True}}, "rules.em-dash"),
                          ({"rules": {"passive": "yes"}}, "rules.passive"),
                          ({"banned": [{"word": ""}]}, "banned entry 1"),
                          ({"reply_default": "loud"}, "reply_default"),
                          ({"colour": 1}, "colour")):
            with self.assertRaises(pl.ConfigError) as cm:
                pl.check_config(bad)
            self.assertIn(word, str(cm.exception))
        with self.assertRaises(pl.ConfigError):
            pl.write_config(self.root, {"limits": {"step": -1}})
        self.assertFalse(pl.config_path(self.root).exists())

    def test_the_shipped_example_checks(self):
        example = pathlib.Path(__file__).resolve().parents[1] / "config" / (
            pl.CONFIG_NAME + ".example")
        (self.root / "config" / pl.CONFIG_NAME).write_text(example.read_text())
        self.assertEqual(pl.load_config(self.root)["banned"][0]["word"], "leverage")


class TestCli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        (self.dir / "config").mkdir()

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            try:
                rc = pl.lint_main(["--root", str(self.dir)] + list(argv))
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_exit_codes_and_human_output(self):
        doc = self.dir / "a.md"
        doc.write_text("Fine.\n")
        self.assertEqual(self.run_cli(str(doc))[0], 0)
        doc.write_text("Fine.\n\nIt was written %s by us.\n" % DASH)
        rc, out, _ = self.run_cli(str(doc))
        self.assertEqual(rc, 1)
        self.assertIn("%s:3:15 em-dash em dash" % doc, out)
        self.assertIn("%s:3:4 passive (note) maybe passive" % doc, out)
        # a note alone does not fail
        self.assertEqual(self.run_cli("--only", "passive", str(doc))[0], 0)
        self.assertEqual(self.run_cli("--only", "nope", str(doc))[0], 2)
        self.assertEqual(self.run_cli(str(self.dir / "missing.md"))[0], 2)
        self.assertEqual(self.run_cli("--write", str(doc))[0], 2)
        self.assertEqual(self.run_cli("--fix", "--write")[0], 2)
        self.assertEqual(self.run_cli("--glossary", str(self.dir / "none.md"), str(doc))[0], 2)
        (self.dir / "config" / pl.CONFIG_NAME).write_text("[limits]\nstep = 0\n")
        self.assertEqual(self.run_cli(str(doc))[0], 2)

    def test_json_stdin_and_glossary(self):
        gloss = self.dir / "g.md"
        gloss.write_text("| Term | Meaning | Do not use |\n|---|---|---|\n| reply | x | response |\n")
        rc, out, _ = self.run_cli("--json", "--glossary", str(gloss), stdin="A response.")
        self.assertEqual(rc, 1)
        rows = json.loads(out)
        self.assertEqual([(r["path"], r["rule"], r["excerpt"]) for r in rows],
                         [("<stdin>", "glossary", "response")])

    def test_fix_prints_and_write_writes(self):
        rc, out, err = self.run_cli("--fix", stdin="a %s b\n" % DASH)
        self.assertEqual((rc, out, err), (0, "a - b\n", ""))
        doc = self.dir / "b.md"
        doc.write_text("x %s y\n\n%s.\n" % (DASH, words(30)))
        rc, out, _ = self.run_cli("--fix", "--write", str(doc))
        self.assertEqual(rc, 1)
        self.assertIn("sentence-length", out)
        self.assertEqual(doc.read_text(), "x - y\n\n%s.\n" % words(30))

    def test_crlf_stays_crlf_and_the_rest_refer_to_the_fixed_text(self):
        doc = self.dir / "c.md"
        doc.write_bytes(("a%sb was written.\r\nok\r\n" % DASH).encode())
        rc, out, _ = self.run_cli("--fix", "--write", str(doc))
        self.assertEqual(doc.read_bytes(), b"a - b was written.\r\nok\r\n")
        self.assertIn("%s:1:7 passive (note) maybe passive" % doc, out)
        self.assertEqual(rc, 0)


class TestReplyModes(HermeticCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "memory").mkdir()
        self.toml = ('[cousin]\nslug = "wren"\nname = "Wren"\n\n[operator]\nname = "Ana"\n'
                     '\n[agent]\nrunner = "fake"\n')
        self.mode(None)
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        os.environ["COUSIN_HOME"] = str(self.home)

    def mode(self, value):
        extra = '\n[plain_language]\nreply = "%s"\n' % value if value else ""
        (self.home / "cousin.toml").write_text(self.toml + extra)

    def ctx(self):
        from cousin_lib.runner.policy import Policy
        turn = Turn()
        turn.begin({"id": 1, "thread_id": "operator:ana", "sender": "Ana"})
        return tools.ToolContext(home=self.home, slug="wren", name="Wren", root=self.root,
                                 turn=turn, policy=Policy.load(self.home), stream=None)

    def sent(self):
        import sqlite3
        conn = sqlite3.connect(self.home / "data" / "chat.db")
        try:
            return [r[0] for r in conn.execute("SELECT message FROM messages ORDER BY id")]
        finally:
            conn.close()

    def test_the_mode_is_the_cousins_then_the_install_default(self):
        self.assertEqual(pl.reply_mode(self.home, self.root), "off")
        (self.root / "config" / pl.CONFIG_NAME).write_text('reply_default = "note"\n')
        self.assertEqual(pl.reply_mode(self.home, self.root), "note")
        self.mode("fix")
        self.assertEqual(pl.reply_mode(self.home, self.root), "fix")
        self.mode("loud")
        self.assertEqual(pl.reply_mode(self.home, self.root), "note")

    def test_reply_off_note_and_fix(self):
        text = "one %s two. It was written." % DASH
        out, err = tools.call(self.ctx(), "reply", {"text": text})
        self.assertFalse(err, out)
        self.assertNotIn("plain language", out)
        self.mode("note")
        out, err = tools.call(self.ctx(), "reply", {"text": text})
        self.assertTrue(out.startswith(tools.REPLIED), out)
        self.assertIn("plain language: 2 findings\n1:4 em-dash", out)
        self.assertIn("passive", out)
        self.mode("fix")
        out, err = tools.call(self.ctx(), "reply", {"text": text})
        self.assertFalse(err, out)
        self.assertIn("plain language: fixed 1 em dash; 1 finding\n", out)
        self.assertEqual(self.sent(), [text, text, "one - two. It was written."])

    def test_a_reply_that_opens_with_a_rule_is_checked_whole(self):
        self.mode("fix")
        text = "---\nOne %s two.\n---\nok" % DASH
        out, err = tools.call(self.ctx(), "reply", {"text": text})
        self.assertFalse(err, out)
        self.assertEqual(self.sent(), ["---\nOne - two.\n---\nok"])

    def test_the_shipped_send_tool_does_not_promise_verbatim(self):
        registry = mcp_server.parse_registry(mcp_server.shipped_default_registry(self.root),
                                             "shipped")
        said = json.dumps(registry["tools"]["send"]).lower()
        self.assertNotIn("verbatim", said)
        self.assertIn("plain-language", said)

    def test_a_check_that_fails_never_blocks_the_reply(self):
        self.mode("fix")
        with mock.patch.object(pl, "check", side_effect=RuntimeError("boom")):
            out, err = tools.call(self.ctx(), "reply", {"text": "a %s b" % DASH})
        self.assertFalse(err, out)
        self.assertEqual(self.sent(), ["a %s b" % DASH])

    def test_send_to_a_peer_fixes_and_notes(self):
        self.mode("fix")
        peer = self.root / "cousins" / "testa"
        peer.mkdir()
        (peer / "cousin.toml").write_text('[cousin]\nslug = "testa"\n[chat]\nport = 8101\n')
        with mock.patch("cousin_lib.chat.send_message", return_value={"ok": True, "id": 3}) as sm:
            out, err = tools.call(self.ctx(), "send", {"to": "testa", "text": "a %s b" % DASH})
        self.assertFalse(err, out)
        self.assertEqual(sm.call_args.args[3], "a - b")
        self.assertEqual(json.loads(out)["plain_language"], "plain language: fixed 1 em dash")

    def test_the_stdio_send_fixes_and_notes(self):
        self.mode("note")
        registry = mcp_server.parse_registry(mcp_server.shipped_default_registry(self.root),
                                             "shipped")
        tool = registry["tools"]["send"]
        env = {"COUSIN_HOME": str(self.home), "FRAMEWORK_ROOT": str(self.root)}
        with mock.patch.object(mcp_server, "run_call", return_value=("sent\n", False)) as rc:
            out, err = mcp_server._send_call(registry, "send", tool,
                                             {"to": "testa", "text": "a %s b" % DASH},
                                             env, frozenset({"testa"}))
        self.assertIn("a %s b" % DASH, rc.call_args.args[0])
        self.assertEqual(out, "sent\nplain language: 1 finding\n1:2 em-dash em dash: use a"
                              " hyphen with spaces ( - ) or two sentences")
        self.mode("fix")
        with mock.patch.object(mcp_server, "run_call", return_value=("sent", False)) as rc:
            mcp_server._send_call(registry, "send", tool, {"to": "testa", "text": "a %s b" % DASH},
                                  env, frozenset({"testa"}))
        self.assertIn("a - b", rc.call_args.args[0])

    def test_fix_mode_notes_refer_to_the_text_that_went(self):
        self.mode("fix")
        text, note = pl.outbound(self.home, self.root, "a%sb was written." % DASH)
        self.assertEqual(text, "a - b was written.")
        self.assertIn("1:7 passive (note) maybe passive", note)

    def test_a_long_text_goes_unchecked_and_says_so(self):
        self.mode("fix")
        text = "a %s b " % DASH * 20000
        out, note = pl.outbound(self.home, self.root, text)
        self.assertEqual(out, text)
        self.assertIn("not checked", note)

    def test_cousin_reply_applies_the_mode(self):
        from cousin_lib.reply import reply_main
        self.mode("fix")
        out = io.StringIO()
        with mock.patch("cousin_lib.server.chat_api.reply", return_value={"id": 5}) as rp, \
                contextlib.redirect_stdout(out):
            rc = reply_main(["--message", "a %s b. It was written." % DASH])
        self.assertEqual(rc, 0)
        self.assertEqual(rp.call_args.args[1]["message"], "a - b. It was written.")
        self.assertIn("plain language: fixed 1 em dash; 1 finding", out.getvalue())

    def test_the_stdio_operator_route_leaves_it_to_cousin_reply(self):
        self.mode("fix")
        registry = mcp_server.parse_registry(mcp_server.shipped_default_registry(self.root),
                                             "shipped")
        tool = dict(registry["tools"]["send"], operators=["Ana"])
        env = {"COUSIN_HOME": str(self.home), "FRAMEWORK_ROOT": str(self.root)}
        with mock.patch.object(mcp_server, "run_call", return_value=("posted", False)) as rc:
            out, _ = mcp_server._send_call(registry, "send", tool,
                                           {"to": "Ana", "text": "a %s b" % DASH}, env, frozenset())
        self.assertEqual(rc.call_args.args[1], ("a %s b" % DASH).encode())
        self.assertEqual(out, "posted")


class TestReviewFixes(unittest.TestCase):
    """Each case a review found, run as it was found."""

    def fixed(self, text):
        return pl.apply_fixes(text, pl.check(text))

    def test_a_dash_after_a_link_keeps_the_link(self):
        text = "See [the docs](https://example.com/docs) %s they help.\n" % DASH
        self.assertEqual(self.fixed(text), "See [the docs](https://example.com/docs) - they help.\n")
        self.assertEqual(self.fixed("![x](a.png)%sy\n" % DASH), "![x](a.png) - y\n")
        # an en dash glued to a link target has no space before it
        self.assertEqual(pl.check("[x](u)%s y" % EN), [])

    def test_fences_in_list_items_and_quotes_and_by_their_own_char(self):
        for text in ("1. Step\n\n    ```\n    x = a %s b\n    ```\n" % DASH,
                     "> ```\n> x = a %s b\n> ```\n" % DASH,
                     "~~~\n```\nx = a %s b\n~~~\n" % DASH,
                     "````\n```\nx = a %s b\n````\n" % DASH):
            self.assertEqual(pl.check(text), [], text)
            self.assertEqual(self.fixed(text), text)
        self.assertEqual(rules(pl.check("~~~\nx\n~~~\na %s b\n" % DASH)), ["em-dash"])

    def test_indented_code_is_skipped_but_list_continuations_are_prose(self):
        code = "Run this:\n\n    x = a %s b\n\tleverage %s y\n\nDone.\n" % (DASH, DASH)
        self.assertEqual(pl.check(code), [])
        self.assertEqual(self.fixed(code), code)
        # four spaces after a list item continue the item: prose, and fixed
        item = "- Step one\n\n    more of it %s here\n" % DASH
        self.assertEqual(rules(pl.check(item)), ["em-dash"])
        # a lazy continuation of a paragraph is prose too
        self.assertEqual(rules(pl.check("Text that\n    goes on %s here\n" % DASH)), ["em-dash"])
        # after the list ends, indented code is code again: a paragraph, a
        # rule, a quote or a table at the margin ends it
        for between in ("Para.", "---", "> quoted", "| a | b |\n|---|---|\n| c | d |"):
            text = "- item\n\n%s\n\n    a %s b\n" % (between, DASH)
            self.assertEqual(pl.check(text), [], between)
            self.assertEqual(self.fixed(text), text, between)

    def test_a_message_starting_with_a_rule_has_no_front_matter(self):
        text = "---\nOne %s two.\n---\nok\n" % DASH
        self.assertEqual(pl.check(text), [])                       # a file: front matter
        self.assertEqual(rules(pl.check(text, front_matter=False)), ["em-dash"])

    def test_an_html_comment_blanks_only_itself(self):
        self.assertEqual(rules(pl.check("<!-- c --> Real %s text\n" % DASH)), ["em-dash"])
        self.assertEqual(pl.check("<!-- a\n%s b -->\n" % DASH), [])
        self.assertEqual(rules(pl.check("x <!-- a\nb --> c %s d\n" % DASH)), ["em-dash"])

    def test_a_heading_term_loses_its_backticks(self):
        self.assertIn("cousin-chat", pl.parse_glossary("### `cousin-chat`\n\nthe CLI.\n"))

    def test_a_dash_that_starts_a_line_is_a_note_without_a_fix(self):
        found = pl.check("Text\n%s not a list\n" % DASH)
        self.assertEqual([(f.rule, f.line, f.col, f.fix, f.severity) for f in found],
                         [("em-dash", 2, 1, None, "note")])
        self.assertEqual(self.fixed("> %s quoted\n" % DASH), "> %s quoted\n" % DASH)

    def test_abbreviations_end_a_sentence_before_a_capital(self):
        self.assertEqual(pl._sentences("Cats etc. Dogs too."), [(0, 9), (9, 19)])
        self.assertEqual(pl._sentences("Cats etc. and dogs. No. 5 is"), [(0, 19), (19, 28)])

    def test_sentence_splitting_is_linear(self):
        import time
        t0 = time.monotonic()
        pl.check("e.g. " * 5000)
        self.assertLess(time.monotonic() - t0, 1.0)




if __name__ == "__main__":
    unittest.main()
