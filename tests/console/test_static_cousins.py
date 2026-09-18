"""The cousins view, the chat header and the shared cousin tag, pinned
by text: the console compiles its JSX in the browser, so these tests
read the files the way the spec-coherence tests do and hold the
surface the operator compared against the source console.
"""
import pathlib
import re
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _component(text, name):
    """The source of one top-level `function <name>(` up to the next
    top-level function, so a check cannot pass on another component."""
    start = text.index("function %s(" % name)
    rest = text[start + 1:]
    m = re.search(r"^function \w+\(", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


class SpawnDialog(unittest.TestCase):
    """The spawn dialog offers model, effort, heartbeat and memory
    scope, reading its catalogue and defaults from GET
    /api/spawn/options and sending the four fields the spawn route
    takes; it carries no catalogue of its own."""

    def setUp(self):
        self.src = _component(_read("cousins.jsx"), "SpawnModal")

    def test_reads_the_catalogue_and_defaults_from_the_options_route(self):
        self.assertIn("/api/spawn/options", self.src)
        for key in ("default_model", "default_effort", "default_heartbeat",
                    "default_memory_scope"):
            self.assertIn(key, self.src, key)

    def test_sends_the_four_runtime_fields(self):
        body = self.src[self.src.index("const body"):]
        for field in ("model", "effort", "heartbeat", "memory_scope"):
            self.assertRegex(body, r"body\.%s\s*=" % field, field)

    def test_offers_the_fields_from_options_not_a_catalogue_of_its_own(self):
        self.assertRegex(self.src, r'label="model"')
        self.assertRegex(self.src, r'label="effort"')
        self.assertRegex(self.src, r'label="heartbeat')
        self.assertRegex(self.src, r'label="memory scope"')
        self.assertIn("radio-row", self.src)
        self.assertRegex(self.src, r"\.models\s*\|\|")
        self.assertRegex(self.src, r"\.efforts\s*\|\|")
        self.assertRegex(self.src, r"\.memory_scopes\s*\|\|")
        # No vendor names, no hard-coded level list to drift from the API.
        self.assertNotRegex(self.src, r'\[\s*"low"\s*,')


class RemoteCousins(unittest.TestCase):
    """Remote cousins (hive nodes): their own card with no start, stop,
    restart or pane control, revoke behind a confirm and forget after
    it; the spawn dialog offers Remote only when the hive is on and
    shows both install commands with copy buttons and the token note;
    the chat header drops the local-only controls."""

    def setUp(self):
        self.cousins = _read("cousins.jsx")
        self.card = _component(self.cousins, "RemoteCousinCard")
        self.form = _component(self.cousins, "RemoteSpawnForm")
        self.modal = _component(self.cousins, "SpawnModal")

    def test_the_view_routes_remote_rows_to_their_own_card(self):
        view = _component(self.cousins, "CousinsView")
        self.assertIn("c.remote ?", view)
        self.assertIn("<RemoteCousinCard", view)
        self.assertIn("/api/hive/nodes/${c.slug}/revoke", view)
        self.assertIn("window.confirm(", view)
        self.assertIn('apiSend("DELETE", `/api/hive/nodes/${c.slug}`)', view)

    def test_the_card_has_no_local_controls(self):
        for word in ('"start"', '"stop"', '"restart"', "pane", "tmux",
                     '"flip"'):
            self.assertNotIn(word, self.card, word)
        for word in ("remote", "last seen", "c.host", "c.port", '"revoke"',
                     '"forget"', "open chat"):
            self.assertIn(word, self.card, word)

    def test_remote_is_offered_only_when_the_hive_is_on(self):
        self.assertIn('apiGet("/api/hive")', self.modal)
        self.assertIn("hive?.enabled &&", self.modal)
        self.assertIn("Remote (another machine)", self.modal)

    def test_the_form_sends_the_build_fields_and_shows_both_commands(self):
        self.assertIn('apiSend("POST", "/api/hive/nodes", body)', self.form)
        for key in ("slug", "name", "role", "brain", "home_chat",
                    "reachable", "body.port", "body.agent_cmd"):
            self.assertIn(key, self.form, key)
        self.assertIn("built.curl", self.form)
        self.assertIn("built.install", self.form)
        self.assertEqual(self.form.count("<CopyButton"), 2)
        self.assertIn("carries the node's bearer token", self.form)

    def test_the_chat_header_drops_local_only_controls_for_remote(self):
        header = _component(_read("chat.jsx"), "ChatHeader")
        self.assertIn("const remote = !!cousin.remote", header)
        self.assertIn("!embed && !remote", header)
        self.assertIn("!paneOpen && !embed && !remote", header)
        view = _component(_read("chat.jsx"), "ChatView")
        self.assertIn("paneOpen && !c.remote", view)


class InspectorAuthField(unittest.TestCase):
    """The inspector's auth control reads the mode names from GET
    /api/cousins/<slug>/auth (no catalogue of its own), takes the key
    in a password field that is cleared as it is sent, and shows only
    whether a key is set and its last four characters."""

    def setUp(self):
        text = _read("cousins.jsx")
        self.src = _component(text, "AuthField")
        self.inspector = _component(text, "Inspector")

    def test_the_inspector_carries_it(self):
        self.assertIn("<AuthField cousin={c} />", self.inspector)

    def test_modes_come_from_the_server(self):
        self.assertIn("/auth`", self.src)
        self.assertRegex(self.src, r"st\.modes\s*\|\|")
        self.assertNotIn('"api_key"', self.src)

    def test_the_key_is_a_password_field_sent_once(self):
        self.assertIn('type="password"', self.src)
        self.assertIn("/auth/key`", self.src)
        # the draft is emptied before the request goes out
        send = self.src[self.src.index("const sendKey"):]
        self.assertLess(send.index('setKeyDraft("")'),
                        send.index("apiSend("))
        self.assertIn("last4", self.src)

    def test_a_busy_refusal_offers_a_forced_restart(self):
        self.assertIn("409", self.src)
        self.assertIn("force", self.src)


if __name__ == "__main__":
    unittest.main()


class CousinCardRows(unittest.TestCase):
    """The card shows the role under the name and the model, pid and
    uptime rows beside the rows it already had; null reads as "-"."""

    def setUp(self):
        self.src = _component(_read("cousins.jsx"), "CousinCard")

    def test_a_row_needing_attention_says_so(self):
        # "running" with the agent parked on a login menu read as
        # healthy; the row's attention field is shown on the card.
        self.assertIn("c.attention", self.src)
        self.assertIn("needs attention", self.src)

    def test_role_sits_under_the_name(self):
        self.assertRegex(self.src, r'className="role"[^\n]*\{c\.role\}')

    def test_model_pid_and_uptime_rows_join_the_existing_ones(self):
        for row in ("chat ·", "scope ·", "operator ·", "beat ·", "flip at ·",
                    "host ·", "model ·", "pid ·", "uptime ·"):
            self.assertIn(row, self.src, row)
        self.assertIn("tokens today", self.src)
        self.assertRegex(self.src, r"pid · <b>\{c\.pid \?\? \"-\"\}")
        self.assertRegex(self.src, r"uptime · <b>\{[^}]*uptime_seconds")
        self.assertRegex(self.src, r"model · <b>\{c\.model")


class ChatHeaderEffort(unittest.TestCase):
    """The chat header carries an effort select that persists through
    POST /api/cousins/<slug>/effort and says a restart applies it; the
    main header shows the model beside the slug."""

    def setUp(self):
        self.chat = _read("chat.jsx")
        self.header = _component(self.chat, "ChatHeader")

    def test_effort_select_persists_and_hints_a_restart(self):
        self.assertIn("/api/spawn/options", self.header)
        self.assertIn("/api/cousins/${cousin.slug}/effort", self.header)
        self.assertRegex(self.header, r"<select[^>]*value=\{effort\}")
        self.assertIn("restart to apply", self.header)
        self.assertRegex(self.header, r"\.efforts\s*\|\|")
        self.assertNotRegex(self.header, r'\[\s*"low"\s*,')

    def test_no_media_filter_returned_with_it(self):
        # The source's "all" dropdown beside the effort select was the
        # media-kind filter; media is out of scope and it stays out.
        self.assertNotIn("chat-media-filter", self.chat)
        self.assertNotIn("any media", self.chat)

    def test_main_header_names_the_model_beside_the_slug(self):
        app = _read("app.jsx")
        self.assertRegex(app, r"\{c\.slug\}[^\n]*c\.model[^\n]*heartbeat")


class CousinTagColour(unittest.TestCase):
    """The fleet-wide cousin tag uses the accent colour. The per-slug
    hash hue served only the log drawer and went with it."""

    def test_tag_uses_the_accent_and_no_hash_hue_is_left(self):
        views = _read("views.jsx")
        tag = _component(views, "CousinTag")
        self.assertIn("var(--accent)", tag)
        for name in ("views.jsx", "data.jsx", "cousins.jsx", "app.jsx"):
            self.assertNotIn("cousinColor", _read(name), name)


class NoTailLogs(unittest.TestCase):
    """The inspector's "tail logs" button opened a drawer polling
    GET /api/logs; it was deprecated and did not work, so the button,
    the drawer, its fetcher, its styles and the route are gone. The
    canary fails if any of them comes back."""

    def test_the_button_and_its_plumbing_are_gone(self):
        inspector = _component(_read("cousins.jsx"), "Inspector")
        self.assertNotIn("tail logs", inspector)
        for name in ("cousins.jsx", "app.jsx", "views.jsx", "data.jsx"):
            text = _read(name)
            for word in ("openLogs", "LogDrawer", "fetchLogs", "/api/logs",
                         "LOG_SEED"):
                self.assertNotIn(word, text, (name, word))
        self.assertNotIn(".logtail", _read("styles.css"))


class PanelEditorsUseTheAccent(unittest.TestCase):
    """The inspector's role and CLAUDE.md textareas showed an amber
    border (the role editor always, the CLAUDE.md editor once dirty)
    while every other field in the console focuses in the accent. They
    now take the shared `.txt` field style, whose focus ring is the
    accent token; the canary fails if an amber border comes back."""

    def setUp(self):
        self.cousins = _read("cousins.jsx")
        self.css = _read("styles.css")

    def test_the_editors_carry_no_amber_border(self):
        for name in ("RoleEditor", "ClaudeMdEditor"):
            src = _component(self.cousins, name)
            self.assertNotRegex(src, r"border[^\n]*var\(--amber\)", name)
            self.assertRegex(src, r'<textarea className="txt[ "]', name)

    def test_the_shared_focus_ring_is_the_accent(self):
        rule = re.search(r"textarea\.txt:focus[^{]*\{([^}]*)\}", self.css)
        self.assertIsNotNone(rule)
        self.assertIn("border-color: var(--accent)", rule.group(1))
        self.assertIn("var(--accent)", rule.group(1).split("box-shadow")[1])


class InspectorIdentityEditors(unittest.TestCase):
    """Operator, memory scope and heartbeat are editable in the
    inspector's identity block through their routes, with the restart
    hint the chat header's effort select uses; the other identity rows
    stay read-only."""

    def setUp(self):
        cousins = _read("cousins.jsx")
        self.inspector = _component(cousins, "Inspector")
        self.field = _component(cousins, "IdentityField")
        self.cousins = cousins

    def test_three_rows_use_the_editor_and_the_rest_do_not(self):
        for field in ("operator", "memory_scope", "heartbeat"):
            self.assertRegex(self.inspector,
                             r'<IdentityField cousin=\{c\} field="%s"' % field)
        self.assertEqual(self.inspector.count("<IdentityField"), 3)
        for row in ("slug", "type", "home", "tmux", "chat", "pid",
                    "uptime"):
            dd = re.search(r"<dt>%s</dt><dd[^>]*>(.*?)</dd>" % row,
                           self.inspector)
            self.assertIsNotNone(dd, row)
            self.assertNotIn("IdentityField", dd.group(1), row)

    def test_routes_limits_and_restart_hint(self):
        for route in ("operator", "memory-scope", "heartbeat"):
            self.assertIn("`/api/cousins/${slug}/%s`" % route, self.cousins)
        self.assertIn("spec.url(cousin.slug)", self.field)
        self.assertIn("restart_required", self.field)
        self.assertIn("restart to apply", self.field)
        for key in ("memory_scopes", "heartbeat_bounds",
                    "operator_max_chars"):
            self.assertIn(key, self.field, key)
        self.assertIn("/api/spawn/options", self.inspector)
        self.assertIn("cancel", self.field)
        self.assertIn("setErr(", self.field)

    def test_heartbeat_reads_in_seconds_and_a_human_form(self):
        beat = _component(self.cousins, "fmtBeat")
        self.assertIn("fmtDuration(", beat)
        self.assertRegex(beat, r"`\$\{n\}s \(")
