"""cousin-doctor identity (cousin_lib/identity_lint.py), over temporary
roots only. Every rule has a line that must be reported and a near miss
that must not: a false positive teaches people to ignore the check."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest

from cousin_lib import doctor, identity_lint, template_sync
from cousin_lib.runner import prompt
from tests._hermetic import HermeticCase

CHECKOUT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = (CHECKOUT / "config" / "mcp-registry.toml.example").read_text()


def registry_without(*tools):
    """The shipped registry with each named tool disabled."""
    text = REGISTRY
    for name in tools:
        text = text.replace("[tools.%s]\n" % name, "[tools.%s]\nenabled = false\n" % name, 1)
    return text


class _Case(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "root"
        (self.root / "config").mkdir(parents=True)

    def cousin(self, slug, *, runner="sdk", account=None, api_key_file=None,
               peer_visible=True, registry=None, claude=None, portrait=None, status=None):
        home = self.root / "cousins" / slug
        home.mkdir(parents=True, exist_ok=True)
        agent = []
        if runner:
            agent.append('runner = "%s"' % runner)
        if account:
            agent.append('account = "%s"' % account)
        if api_key_file:
            agent.append('api_key_file = "%s"' % api_key_file)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\npeer_visible = %s\n\n[agent]\n%s\n'
            % (slug, slug.capitalize(), "true" if peer_visible else "false", "\n".join(agent)))
        if registry is not None:
            (home / "mcp-registry.toml").write_text(registry)
        for name, text in (("CLAUDE.md", claude), ("self-portrait.md", portrait),
                           ("STATUS.md", status)):
            if text is not None:
                (home / name).write_text(text)
        return home

    def accounts(self, text):
        (self.root / "config" / "accounts.toml").write_text(text)

    def below(self, *lines):
        """A CLAUDE.md whose authored lines sit below the marker."""
        return "# Wren - helper\n\n%s\n\n%s\n" % (template_sync.MARKER, "\n".join(lines))

    def items(self, cousin=None):
        result = doctor.check_identity(self.root, cousin=cousin)
        return result["items"]

    def rules(self, cousin=None):
        return [(i["rule"], i["line"]) for i in self.items(cousin)]


# ---------------------------------------------------------------- lane

class TestLane(_Case):
    def test_cousin_reply_on_a_runner_lane_names_the_reply_tool(self):
        self.cousin("wren", claude=self.below("Answer Ana with `cousin-reply --user Ana`."))
        [item] = self.items()
        self.assertEqual(item["rule"], "lane")
        self.assertEqual(item["file"], "cousins/wren/CLAUDE.md")
        self.assertEqual(item["line"], 5)
        self.assertEqual(item["text"], "Answer Ana with `cousin-reply --user Ana`.")
        self.assertIn("sdk runner", item["fact"])
        self.assertEqual(item["fix"], "use the `mcp__cousin__reply` tool instead of `cousin-reply`")

    def test_the_registry_gives_each_cli_its_tool_and_command(self):
        self.cousin("wren", claude=self.below(
            "Message a peer: cousin-chat send sam 'hi'",
            "Log it: `cousin-memory decide --stdin`",
            "Track it with cousin-job start subagent -- title",
            "Long runs: cousin-job start shell -- t cmd"))
        fixes = [i["fix"] for i in self.items()]
        self.assertEqual(fixes, [
            "use the `mcp__cousin__send` tool instead of `cousin-chat send`",
            "use the `mcp__cousin__memory` tool's `decide` command instead of"
            " `cousin-memory decide`",
            "use the `mcp__cousin__job` tool's `start` command instead of `cousin-job start`",
            "use the `mcp__cousin__job` tool's `run` command instead of"
            " `cousin-job start shell`"])

    def test_the_opencode_lane_names_its_own_tools(self):
        self.cousin("wren", runner="opencode",
                    claude=self.below("Reply with cousin-reply --user Ana."))
        [item] = self.items()
        self.assertIn("`cousin_reply`", item["fix"])

    def test_no_runner_lane_no_finding(self):
        self.cousin("wren", runner=None, claude=self.below("Reply with cousin-reply --user Ana."))
        self.assertEqual(self.items(), [])

    def test_a_negated_or_fallback_line_is_not_reported(self):
        self.cousin("wren", claude=self.below(
            "Never answer with `cousin-reply` through Bash.",
            "If the reply tool fails, fall back to cousin-reply --user Ana.",
            "The old cousin-memory decide habit is gone.",
            "Reply with the reply tool. Not with cousin-chat send."))
        self.assertEqual(self.items(), [])

    def test_a_cli_command_no_tool_serves_is_not_reported(self):
        self.cousin("wren", claude=self.below(
            "Rebuild the views with cousin-memory distill.",
            "List peers with cousin-chat list.",
            "The cousin-reply.py script is gone."))
        self.assertEqual(self.items(), [])

    def test_a_line_about_a_commands_syntax_is_not_reported(self):
        self.cousin("wren", portrait=(
            "`cousin-chat send` takes the peer then the text and `cousin-reply` takes --user"
            " first. Verify a CLI's shape from --help before piping.\n"
            "cousin-reply reads its arguments from argv, not stdin.\n"
            "The usage line of cousin-chat send names the flags.\n"))
        self.assertEqual(self.items(), [])

    def test_a_wrapped_note_about_a_commands_syntax_is_not_reported(self):
        # the usage words are on the bullet's second physical line
        self.cousin("wren", portrait=(
            "- `cousin-chat send` takes the peer then the text, and `cousin-reply` takes\n"
            "  --user first. Verify a CLI's shape from --help before piping.\n"))
        self.assertEqual(self.items(), [])

    def test_a_new_bullet_is_not_joined_to_the_one_before(self):
        self.cousin("wren", portrait=(
            "- Verify the shape of the backup before trusting it\n"
            "- Answer the operator with cousin-reply.\n"))
        self.assertEqual([i["rule"] for i in self.items()], ["lane"])

    def test_a_distinction_between_commands_is_kept_in_tool_terms(self):
        self.cousin("wren", claude=self.below(
            "`cousin-reply` is for the operator only, peers get `cousin-chat send`.",
            "Every peer message reaches you via cousin-chat send.",
            "All replies reach Ana via cousin-reply.",
            "`cousin-chat send <peer>` = peer messages only."))
        items = self.items()
        self.assertEqual([(i["line"], i["match"]) for i in items],
                         [(5, "cousin-reply"), (5, "cousin-chat send"), (6, "cousin-chat send"),
                          (7, "cousin-reply"), (8, "cousin-chat send")])
        send = ("rewrite the distinction in tool terms: `cousin-chat send` -> the"
                " `mcp__cousin__send` tool")
        both = ("rewrite the distinction in tool terms: `cousin-reply` -> the"
                " `mcp__cousin__reply` tool, `cousin-chat send` -> the `mcp__cousin__send` tool")
        self.assertEqual([i["fix"] for i in items], [both, both, send,
                         "rewrite the distinction in tool terms: `cousin-reply` -> the"
                         " `mcp__cousin__reply` tool", send])

    def test_a_long_line_is_shown_around_its_match(self):
        filler = "The garden notes go in the shed and the seed list stays current. " * 4
        self.cousin("wren", claude=self.below(filler + "Reply with `cousin-reply --user Ana`. "
                                              + filler))
        [item] = self.items()
        self.assertEqual(item["match"], "cousin-reply")
        self.assertIn("Reply with `cousin-reply --user Ana`.", item["text"])
        self.assertTrue(item["text"].startswith("...") and item["text"].endswith("..."))
        self.assertEqual(len(item["text"]), identity_lint._MAX_SHOWN)

    def test_a_match_near_the_end_of_a_long_line_cuts_only_the_start(self):
        filler = "The garden notes go in the shed and the seed list stays current. " * 4
        self.cousin("wren", claude=self.below(filler + "Reply with cousin-reply."))
        [item] = self.items()
        self.assertTrue(item["text"].startswith("..."))
        self.assertTrue(item["text"].endswith("Reply with cousin-reply."))
        self.assertEqual(len(item["text"]), identity_lint._MAX_SHOWN)

    def test_status_md_is_read_in_its_live_open_loops_only(self):
        self.cousin("wren", status="# Status\n\n## Open loops\n\n- reply with cousin-reply\n\n"
                                    "## Log\n\n- answer with cousin-reply\n")
        self.assertEqual([(i["file"], i["line"]) for i in self.items()],
                         [("cousins/wren/STATUS.md", 5)])

    def test_a_history_line_is_not_reported(self):
        self.cousin("wren", status="# Status\n\n## Open loops\n\n"
                                    "- Replied via cousin-chat send sam at noon.\n"
                                    "- Logged it with cousin-memory decide.\n")
        self.assertEqual(self.items(), [])

    def test_a_cli_whose_tool_is_disabled_is_the_fallback(self):
        self.cousin("wren", registry=registry_without("memory"),
                    claude=self.below("Log it with cousin-memory decide --stdin."))
        self.assertEqual(self.items(), [])

    def test_the_framework_sections_of_claude_md_are_not_read(self):
        text = ("# Wren - helper\n\n## Talking to people\n\nReply with cousin-reply --user Ana.\n\n"
                "## Identity\n\nI keep notes.\n\n%s\n" % template_sync.MARKER)
        self.cousin("wren", claude=text)
        self.assertEqual(self.items(), [])

    def test_a_template_paragraph_below_the_marker_is_not_read(self):
        home = self.cousin("wren", claude="")
        rendered = template_sync._render(template_sync._template_text(self.root),
                                         template_sync._values(home))
        para = next(p for p in prompt._paragraphs(rendered)
                    if "cousin-reply" in p and "{{" not in p)
        (home / "CLAUDE.md").write_text(self.below(para, "", "Reply with cousin-reply now."))
        [item] = self.items()
        self.assertEqual(item["text"], "Reply with cousin-reply now.")

    def test_the_lines_read_are_the_identity_the_prompt_composes(self):
        text = ("# Wren - helper\n\n## Identity\n\nI keep notes.\n<!-- a\ncomment -->\n"
                "I answer briefly.\n\n## Tools\n\nNot identity.\n\n## Voice\n\nPlain.\n\n"
                "%s\n\n## Mine\n\nMy own section.\n" % template_sync.MARKER)
        read = identity_lint.claude_lines(text, set())
        composed = prompt._claude_identity(text, set())
        self.assertEqual(" ".join(" ".join(line for _n, line in read).split()),
                         " ".join(composed.replace("## Identity", "").replace("## Voice", "")
                                  .split()))
        self.assertEqual([n for n, _l in read], [1, 5, 8, 16, 20, 22])


# ---------------------------------------------------------------- billing

class TestBilling(_Case):
    TITLE = "# Wren - helper on the operator's personal subscription, no company key\n"

    def test_a_subscription_claim_on_a_key_account_is_reported(self):
        self.accounts('[accounts.work]\nkind = "anthropic-key"\n')
        self.cousin("wren", account="work", claude=self.TITLE)
        [item] = self.items()
        self.assertEqual((item["rule"], item["line"]), ("billing", 1))
        self.assertIn("account 'work', kind anthropic-key: an API key", item["fact"])
        self.assertIn("config/accounts.toml", item["fact"])
        self.assertEqual(item["fix"], "reword the line to say it runs on an API key, or remove it")

    def test_an_api_key_file_is_a_key_account_too(self):
        self.cousin("wren", api_key_file=".secrets/wren.env", portrait="I use no API key.\n")
        [item] = self.items()
        self.assertEqual(item["file"], "cousins/wren/self-portrait.md")
        self.assertIn("api_key_file", item["fact"])

    def test_a_billing_claim_past_the_cut_is_shown(self):
        filler = "Wren keeps the garden notes and the seed list for the household. " * 4
        self.cousin("wren", api_key_file=".secrets/wren.env",
                    portrait=filler + "It runs on the personal subscription.\n")
        [item] = self.items()
        self.assertEqual(item["match"], "on the personal subscription")
        self.assertIn(item["match"], item["text"])
        self.assertTrue(item["text"].startswith("..."))

    def test_the_same_claim_on_a_subscription_login_is_not_reported(self):
        self.cousin("wren", claude=self.TITLE)
        self.assertEqual(self.items(), [])

    def test_a_key_claim_on_a_subscription_login_is_reported(self):
        self.cousin("wren", status="# Status\n\n## Open loops\n\nWren is billed to the company"
                                    " API key.\n")
        [item] = self.items()
        self.assertEqual((item["file"], item["line"]), ("cousins/wren/STATUS.md", 5))
        self.assertIn("kind claude-login: a subscription login", item["fact"])

    def test_history_comparison_and_other_peoples_plans_are_not_reported(self):
        self.accounts('[accounts.work]\nkind = "anthropic-key"\n')
        self.cousin("wren", account="work", claude=self.below(
            "Wren no longer runs on a personal subscription.",
            "It used to run on the subscription login.",
            "Ana pays for her personal subscription herself.",
            "Moved off a company key: no API key since, on a subscription now, then on an API"
            " key again."))
        self.assertEqual(self.items(), [])


# ---------------------------------------------------------------- peer

class TestPeer(_Case):
    def test_a_peer_said_unable_to_message_it_when_it_can_is_reported(self):
        self.cousin("wren")
        self.cousin("sam", claude=self.below("Wren cannot message me since the move."))
        [item] = self.items()
        self.assertEqual((item["cousin"], item["rule"]), ("sam", "peer"))
        self.assertIn("wren can message sam", item["fact"])
        self.assertIn("serves `send`", item["fact"])

    def test_a_peer_that_really_cannot_is_not_reported(self):
        self.cousin("wren", peer_visible=False)
        self.cousin("sam", claude=self.below("Wren cannot message me."))
        self.assertEqual(self.items(), [])

    def test_a_peer_without_the_send_tool_is_not_reported(self):
        self.cousin("wren", registry=registry_without("send"))
        self.cousin("sam", claude=self.below("Wren can't reach us."))
        self.assertEqual(self.items(), [])

    def test_other_objects_conditions_and_strangers_are_not_reported(self):
        self.cousin("wren")
        self.cousin("sam", claude=self.below(
            "Wren cannot reach the internet.",
            "Wren cannot message me while its runner is stopped.",
            "Kit cannot message me.",
            "Wren never said it cannot message me."))
        self.assertEqual(self.items(), [])


# ---------------------------------------------------------------- tool

class TestTool(_Case):
    def test_a_tool_the_registry_does_not_serve_is_reported(self):
        self.cousin("wren", claude=self.below("Post with mcp__cousin__chat_send."))
        [item] = self.items()
        self.assertEqual(item["rule"], "tool")
        self.assertIn("serves no `chat_send`", item["fact"])
        self.assertIn("handoff, job, meeting, memory, reply, schedule, send", item["fact"])

    def test_a_disabled_tool_is_reported(self):
        self.cousin("wren", registry=registry_without("memory"),
                    claude=self.below("Search with mcp__cousin__memory first."))
        self.assertEqual(self.rules(), [("tool", 5)])

    def test_served_tools_and_other_servers_are_not_reported(self):
        self.cousin("wren", claude=self.below(
            "Answer with mcp__cousin__reply, keep with mcp__cousin__memory.",
            "Mail goes through mcp__mail__send; any mcp__cousin__* tool is fine."))
        self.assertEqual(self.items(), [])


# ---------------------------------------------------------------- the check

class TestCheck(_Case):
    def test_one_cousin_is_checked_and_the_others_are_still_peers(self):
        self.cousin("wren", claude=self.below("Reply with cousin-reply."))
        self.cousin("sam", claude=self.below("Wren cannot message me."))
        self.assertEqual({i["cousin"] for i in self.items()}, {"wren", "sam"})
        self.assertEqual([i["cousin"] for i in self.items("sam")], ["sam"])

    def test_summary_counts_lines_cousins_and_rules(self):
        self.cousin("wren", claude=self.below("Reply with cousin-reply.",
                                              "Or mcp__cousin__nope."))
        self.cousin("sam")
        result = doctor.check_identity(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"],
                         "2 lines contradict the framework in 1 of 2 cousins (lane 1, tool 1)")

    def test_summary_counts_one_claim_repeated_once_as_distinct(self):
        self.accounts('[accounts.work]\nkind = "anthropic-key"\n')
        self.cousin("wren", account="work",
                    claude="# Wren - helper on a personal subscription\n",
                    portrait="Runs on a Personal  Subscription.\nI use no API key.\n",
                    status="# Status\n\n## Open loops\n\n- on a personal subscription\n")
        self.cousin("sam", account="work", portrait="Sam is on a personal subscription.\n")
        result = doctor.check_identity(self.root)
        self.assertEqual(len(result["items"]), 5)
        self.assertEqual(result["summary"], "5 lines contradict the framework in 2 of 2 cousins"
                                            " (billing 5 (3 distinct))")

    def test_clean_identities_are_ok(self):
        self.cousin("wren", claude=self.below("I keep the garden notes."))
        result = doctor.check_identity(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"],
                         "1 cousin, no identity line contradicts the framework")

    def test_an_unknown_account_skips_billing_with_a_note(self):
        self.cousin("wren", account="nosuch", claude="# Wren - on a company key\n")
        result = doctor.check_identity(self.root)
        self.assertTrue(result["ok"])
        self.assertIn("the billing rule is skipped", " ".join(result["notes"]))

    def test_the_check_writes_nothing(self):
        home = self.cousin("wren", claude=self.below("Reply with cousin-reply."))
        before = sorted((p, p.stat().st_mtime_ns) for p in self.root.rglob("*"))
        doctor.check_identity(self.root)
        self.assertEqual(sorted((p, p.stat().st_mtime_ns) for p in self.root.rglob("*")), before)
        self.assertTrue(home.is_dir())


class TestMain(_Case):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = doctor.doctor_main(list(argv) + ["--root", str(self.root)])
        return rc, out.getvalue(), err.getvalue()

    def test_a_finding_prints_line_fact_and_fix_and_exits_1(self):
        self.cousin("wren", claude=self.below("Reply with cousin-reply."))
        rc, out, _ = self.main("identity")
        self.assertEqual(rc, 1)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("WARN  identity: 1 line contradicts"))
        self.assertIn("      cousins/wren/CLAUDE.md:5 [lane] Reply with cousin-reply.", lines)
        self.assertTrue(any(line.startswith("        fact: wren runs on the sdk runner")
                            for line in lines))
        self.assertIn("        fix:  use the `mcp__cousin__reply` tool instead of"
                      " `cousin-reply`", lines)

    def test_cousin_limits_every_check(self):
        os.chmod(self.cousin("wren", claude=self.below("Reply with cousin-reply.")), 0o755)
        os.chmod(self.cousin("sam"), 0o700)
        rc, out, _ = self.main("--cousin", "sam", "--json")
        self.assertEqual(rc, 0, out)
        homes, identity = json.loads(out)
        self.assertEqual(homes["summary"], "1 cousin home, none open to group or other")
        self.assertEqual(identity["items"], [])

    def test_an_unknown_cousin_is_exit_2(self):
        self.cousin("wren")
        rc, _, err = self.main("identity", "--cousin", "kit")
        self.assertEqual(rc, 2)
        self.assertIn("no cousin 'kit'", err)


if __name__ == "__main__":
    unittest.main()
