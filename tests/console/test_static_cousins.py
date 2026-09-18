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
