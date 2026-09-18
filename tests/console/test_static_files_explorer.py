"""The cousin file explorer and the shared file viewer, pinned by text
(the console compiles its JSX in the browser)."""
import pathlib
import re
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _component(text, name):
    start = text.index("function %s(" % name)
    rest = text[start + 1:]
    m = re.search(r"^(function \w+\(|const \w+ = )", rest, re.M)
    return text[start:start + 1 + (m.start() if m else len(rest))]


class CousinFileExplorer(unittest.TestCase):
    def test_the_inspector_opens_the_file_explorer(self):
        src = _component(_read("cousins.jsx"), "Inspector")
        self.assertIn("<CousinFilesModal slug={c.slug}", src)

    def test_the_tree_is_lazy_and_dotfiles_are_a_toggle(self):
        node = _component(_read("explorer.jsx"), "FileTreeNode")
        self.assertIn("/api/cousins/${slug}/files?path=", node)
        files = _component(_read("explorer.jsx"), "CousinFiles")
        self.assertIn("hidden=1", files)
        self.assertIn("/files/download?path=", files)

    def test_explorer_jsx_loads_after_chat_and_before_cousins(self):
        html = _read("index.html")
        order = [html.index('src="%s"' % n) for n in
                 ("chat.jsx", "explorer.jsx", "cousins.jsx", "views.jsx")]
        self.assertEqual(order, sorted(order))

    def test_markdown_is_sanitized_before_it_reaches_the_dom(self):
        doc = _component(_read("explorer.jsx"), "MarkdownDoc")
        self.assertIn("sanitizeHtml(renderMarkdown(", doc)
        clean = _component(_read("explorer.jsx"), "sanitizeHtml")
        for needle in ("script", 'startsWith("on")', "javascript:"):
            self.assertIn(needle, clean)


if __name__ == "__main__":
    unittest.main()
