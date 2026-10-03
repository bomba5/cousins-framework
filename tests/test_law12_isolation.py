"""Law 12: a private cousin's content never reaches another cousin.

One install, two cousins. wren is private ([memory] scope = "private")
and carries a marker word in every place a cousin keeps content: raw
memory written through the real writers, the distilled views, a memory
file, the memory index, notes, the harness auto-memory directory,
STATUS.md, the handoff, the active threads, its identity, its
operator's standing rules, its tool trace and a pending shared-tier
proposal. sam is the other cousin, with a marker of its own.

For sam, every surface the runner builds or reads is composed through
the library entry points the runner uses (the system prompt, the tmux
context block, the state digest, the trace summary, memory search and
recall through the runner's tool handlers, the prompt-time recall hook,
the shared tier) and wren's marker must appear in none of them. Each
surface is also composed for wren and must carry wren's marker, so an
empty surface can never pass for an isolated one.
"""
import json
import os
import pathlib
import tempfile
from unittest import mock

from cousin_lib import boot, mcp_server, memory, memory_search, shared_tier, trace
from cousin_lib.runner import hooks, prompt, tools
from cousin_lib.config import expand_harness_path
from tests._hermetic import HermeticCase

REPO = pathlib.Path(__file__).resolve().parents[1]
MARK = "zephyrquill"        # wren's private word
OWN = "lanternmoss"         # sam's own word
PROPOSAL = "reference_ledger-codes.md"


def _toml(slug, scope):
    return ('[cousin]\nslug = "%s"\nname = "%s"\n\n[memory]\nscope = "%s"\n'
            'recall_keyword_only = true\n' % (slug, slug.capitalize(), scope))


class TwoCousins(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        config = self.root / "config"
        config.mkdir()
        (config / "law.md").write_text("12. A private cousin's content stays its own.\n")
        (config / "harness.toml").write_text(
            'auto_memory_dir = "%s/harness/{home_encoded}/memory"\n' % self.root)
        (config / "shared-reviewers.json").write_text(json.dumps({"reviewers": ["ana"]}))
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "rule_plain.md").write_text("---\nkind: rule\n---\nWrite plainly.\n")
        (shared / "reference_map.md").write_text("---\ndescription: the floor map\n---\nx\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)})
        p.start(); self.addCleanup(p.stop)
        self.registry = mcp_server.parse_registry(
            mcp_server.shipped_default_registry(REPO), "t")
        self.wren = self._cousin("wren", "private", MARK)
        self.sam = self._cousin("sam", "shared", OWN)
        # law 11 now refuses a proposal from a private cousin, so this is
        # one written before that check: the pending file, as propose left it
        proposed = shared / "proposed"
        proposed.mkdir(exist_ok=True)
        (proposed / ("wren__%s" % PROPOSAL)).write_text(
            "---\ndescription: ledger codes %s\n---\nThe codes are %s.\n" % (MARK, MARK))

    def _cousin(self, slug, scope, word):
        """A home with `word` in every place a cousin keeps content."""
        home = self.root / "cousins" / slug
        for sub in ("data", "memory/distilled", "notes"):
            (home / sub).mkdir(parents=True)
        (home / "cousin.toml").write_text(_toml(slug, scope))
        (home / "CLAUDE.md").write_text(
            "# %s - keeps the %s ledger\n\n## Identity\n\n%s keeps the %s ledger.\n"
            % (slug.capitalize(), word, slug.capitalize(), word))
        (home / "self-portrait.md").write_text("# Portrait\n## Voice\nSays %s often.\n" % word)
        (home / "STATUS.md").write_text("# %s\n\n## Open loops\n\n- reconcile the %s ledger\n"
                                        % (slug, word))
        (home / "data" / "handoff.md").write_text("# Handoff\nmid %s reconciliation\n" % word)
        (home / "data" / "active-threads.md").write_text("# Threads\n- %s: drafting\n" % word)
        (home / "MEMORY.md").write_text("# Memory index\n- %s ledger notes\n" % word)
        (home / "memory" / "reference_vault.md").write_text(
            "# Vault %s\nThe vault phrase is %s.\n" % (word, word))
        (home / "memory" / "distilled" / "project-facts.md").write_text(
            "# Project Facts\n- the %s ledger closes monthly\n" % word)
        (home / "notes" / "plan.md").write_text("# Plan for %s\nMove the %s ledger.\n" % (word, word))
        harness = expand_harness_path(
            "%s/harness/{home_encoded}/memory" % self.root, home)
        harness.mkdir(parents=True)
        (harness / "note.md").write_text("# Harness note\n%s remembered here.\n" % word)
        memory.remember(home, "ledger vault", "The vault phrase is %s." % word)
        memory.decide(home, "ledger order", "Close the %s ledger first" % word,
                      "the %s ledger feeds the rest" % word)
        memory.remember(home, "rule: ledger wording", "Always call it the %s ledger." % word,
                        level="operator", cite="chat 7")
        trace.log_call(slug, "cousin-memory", args_summary="search %s" % word,
                       result_summary="%s found" % word)
        return home

    # -- helpers ---------------------------------------------------------

    def ctx(self, home):
        return tools.ToolContext(home=home, slug=home.name, name=home.name.capitalize(),
                                 root=self.root, turn=None, policy=None)

    def assertIsolated(self, surface, sam_text, wren_text, word=MARK):
        """sam's text lacks wren's word; wren's own text carries it."""
        self.assertIn(word, wren_text, "%s: wren's own surface lacks its content,"
                      " so the check would prove nothing" % surface)
        self.assertNotIn(word, sam_text, "%s: wren's private content reached sam" % surface)


class TestBootSurfaces(TwoCousins):
    def test_the_system_prompt_carries_none_of_the_other_cousins_content(self):
        # enforces: law 12
        def compose(home):
            return prompt.compose_system_prompt(home, root=self.root, registry=self.registry,
                                                version="1.0.0")
        self.assertIsolated("system prompt", compose(self.sam), compose(self.wren))
        self.assertIn(OWN, compose(self.sam))

    def test_the_tmux_context_block_carries_none_of_it(self):
        # enforces: law 12
        def block(home):
            return prompt.compose_context_block(home, root=self.root, registry=self.registry,
                                                version="1.0.0")
        self.assertIsolated("tmux context block", block(self.sam), block(self.wren))

    def test_the_state_digest_carries_none_of_it(self):
        # enforces: law 12
        def digest(home):
            return prompt.state_digest(home, root=self.root, slug=home.name,
                                       generation=2)["text"]
        sam = digest(self.sam)
        self.assertIsolated("state digest", sam, digest(self.wren))
        self.assertIn(OWN, sam)

    def test_the_trace_summary_is_the_cousins_own(self):
        # enforces: law 12
        sam = trace.summary_for_boot("sam", root=self.root)
        self.assertIsolated("trace summary", sam, trace.summary_for_boot("wren", root=self.root))
        self.assertIn(OWN, sam)


class TestRetrieval(TwoCousins):
    def test_search_finds_nothing_of_the_other_cousin(self):
        # enforces: law 12
        for collection in (None, "memory", "notes", "harness", "raw"):
            def hits(home):
                found, _notice = memory_search.search(MARK, top=20, home=home,
                                                      collection=collection, root=self.root,
                                                      record=False)
                return found
            wren, sam = hits(self.wren), hits(self.sam)
            self.assertIsolated("search %s" % collection, json.dumps(sam), json.dumps(wren))
            for hit in sam:
                self.assertNotIn(str(self.wren), str(hit["path"]))

    def test_the_runner_search_tool_finds_nothing_of_it(self):
        # enforces: law 12
        def search(home, **extra):
            return tools._m_search(self.ctx(home), dict({"query": MARK, "top": 20}, **extra))
        self.assertIsolated("search tool", search(self.sam), search(self.wren))
        self.assertIsolated("search tool json", search(self.sam, json=True),
                            search(self.wren, json=True))
        own = tools._m_search(self.ctx(self.sam), {"query": OWN, "top": 20})
        self.assertIn(OWN, own)
        self.assertNotIn(MARK, own)

    def test_the_runner_recall_tool_recalls_nothing_of_it(self):
        # enforces: law 12
        for keyword in (MARK, "ledger", ""):
            def recall(home):
                text = tools._m_recall(self.ctx(home), {"keyword": keyword, "last": 50})
                return text.replace("matching '%s'" % keyword, "")    # the echoed query
            self.assertIsolated("recall %r" % keyword, recall(self.sam), recall(self.wren))
        self.assertIn(OWN, tools._m_recall(self.ctx(self.sam), {"keyword": "", "last": 50}))

    def test_prompt_time_recall_surfaces_nothing_of_it(self):
        # enforces: law 12
        body = "where is the %s ledger and the vault phrase kept" % MARK
        sam_text, _n = hooks.default_recall(self.sam, self.root)(body)
        wren_text, _n = hooks.default_recall(self.wren, self.root)(body)
        self.assertIsolated("prompt recall", sam_text or "", wren_text or "")
        self.assertNotIn(str(self.wren), sam_text or "")
        context = memory_search.recall_context(self.sam, body, root=self.root)
        self.assertNotIn(MARK, context or "")
        self.assertNotIn(str(self.wren), context or "")


class TestSharedTier(TwoCousins):
    def other(self):
        """What sam composes and reads of the shared tier."""
        rules, index = boot.shared_parts(self.root)
        listing = shared_tier.list_shared()
        canonical = "".join(shared_tier.read_shared(n) for n in listing["canonical"])
        return "\n".join(rules + index + [canonical, prompt.state_digest(
            self.sam, root=self.root, slug="sam", generation=2)["text"],
            prompt.compose_system_prompt(self.sam, root=self.root, registry=self.registry,
                                         version="1.0.0")])

    def test_a_private_cousin_never_nominates_its_memory(self):
        # enforces: law 12
        plan = shared_tier.plan_bulk_propose(self.wren, "wren")
        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["propose"], [])

    def test_a_pending_proposal_reaches_no_shared_read_until_promoted(self):
        # enforces: law 12
        self.assertNotIn(MARK, self.other())
        self.assertNotIn(PROPOSAL, shared_tier.list_shared()["canonical"])
        shared_tier.promote(PROPOSAL, proposer="wren", by="ana")
        self.assertIn(MARK, self.other())      # promoted: shared on purpose

    def test_a_shared_read_stays_inside_the_shared_tier(self):
        """`cousin-shared read <file>` and `diff <file>` join the name onto
        shared/; a name that leaves shared/ (a `..` or an absolute path)
        must not read another cousin's home. The console's content route
        already refuses such a name."""
        # enforces: law 12
        for name in ("../cousins/wren/notes/plan.md",
                     "../cousins/wren/memory/reference_vault.md",
                     str(self.wren / "STATUS.md")):
            try:
                text = shared_tier.read_shared(name)
            except (OSError, ValueError):
                text = ""
            self.assertNotIn(MARK, text, "cousin-shared read %s" % name)
            try:
                diff = shared_tier.diff_proposal(name, "sam")
            except (OSError, ValueError):
                diff = ""
            self.assertNotIn(MARK, diff, "cousin-shared diff %s" % name)
