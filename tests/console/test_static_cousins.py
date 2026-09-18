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


if __name__ == "__main__":
    unittest.main()


class CousinCardRows(unittest.TestCase):
    """The card shows the role under the name and the model, pid and
    uptime rows beside the rows it already had; null reads as "-"."""

    def setUp(self):
        self.src = _component(_read("cousins.jsx"), "CousinCard")

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
