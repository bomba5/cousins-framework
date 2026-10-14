"""The console keeps its view and cousin in the address, pinned by text
(the page compiles its JSX in the browser and has no build step).

The failure it holds off: every reload landed on Overview, so a phone
app whose WebView was restored after the system reclaimed it lost the
chat you had open.
"""
import pathlib
import re
import unittest

_APP = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static" / "app.jsx"


class ViewInAddress(unittest.TestCase):

    def setUp(self):
        src = _APP.read_text(encoding="utf-8")
        m = re.search(r"useEffectApp\(\(\) => \{\n(?:(?!\n  \}, \[).)*"
                      r"replaceState(?:(?!\n  \}, \[).)*\n  \}, \[([^\]]*)\]\);",
                      src, re.S)
        self.assertIsNotNone(m, "no effect writes the address")
        self.effect, self.deps = m.group(0), m.group(1)

    def test_follows_view_and_cousin(self):
        self.assertEqual([d.strip() for d in self.deps.split(",")],
                         ["view", "activeCousin"])
        self.assertIn('p.set("view", view)', self.effect)
        self.assertIn('p.set("cousin", activeCousin)', self.effect)

    def test_replaces_never_pushes(self):
        self.assertNotIn("pushState", self.effect)

    def test_keeps_the_other_parameters(self):
        # it starts from the current query, so ?user= and ?embed= survive
        self.assertIn("new URLSearchParams(window.location.search)", self.effect)


if __name__ == "__main__":
    unittest.main()
