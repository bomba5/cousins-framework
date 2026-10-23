"""The skills package's browser side (skills.jsx): registered through the
seams only (a System tab and an inspector panel), approve and remove
post the sha256 of the diff the page showed, and the file keeps the
console's style rules. Pinned by text."""
import pathlib
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" / "console_static"


class SkillsJsx(unittest.TestCase):
    def setUp(self):
        self.src = (_STATIC / "skills.jsx").read_text(encoding="utf-8")

    def test_registered_through_the_seams(self):
        self.assertIn('registerSlot("system.tabs", { id: "skills", label: "skills", order: 45',
                      self.src)
        self.assertIn('registerSlot("inspector.panels", { id: "skills"', self.src)
        self.assertNotIn("registerView(", self.src)

    def test_approve_and_remove_send_the_diff_that_was_shown(self):
        self.assertIn("`${base}/approve`, { diff_sha256: d.diff_sha256 }", self.src)
        self.assertIn("skillPost(removeUrl, { diff_sha256: removal.diff_sha256 })", self.src)
        # both ask twice
        self.assertGreaterEqual(self.src.count("<SysConfirmButton"), 3)

    def test_it_says_where_a_private_cousins_skills_are(self):
        self.assertIn("a private cousin's are in its inspector", self.src)
        self.assertIn('data.lane === "opencode"', self.src)

    def test_it_shows_health_and_starts_a_staleness_run(self):
        self.assertIn('<SkillsCheckPanel run={data.check_run}', self.src)
        self.assertIn('skillPost("/api/skills/check")', self.src)
        self.assertIn('{"check" in item && <SkillHealth item={item}', self.src)
        self.assertIn("upstream changed", self.src)

    def test_the_operator_imports_from_the_inspector(self):
        self.assertIn("skillPost(`/api/cousins/${skillPath(slug)}/skills/import`, body)", self.src)
        self.assertIn('<SkillImportForm slug={c.slug} targets={data.import_targets} onDone={done} />',
                      self.src)
        self.assertIn("import a skill from outside...", self.src)
        # only where the import lands, and every key the route takes
        self.assertIn("{(where[f.scope] || []).map(r => <option", self.src)
        self.assertIn('{scopes.map(s => <option key={s} value={s}>{s}</option>)}', self.src)
        self.assertNotIn('["personal", "private"].map', self.src)
        for key in ("hosts", "reads", "rests_on", "license"):
            self.assertIn('{field("%s", ' % key, self.src)
        self.assertIn("no repository this cousin loads takes a skill from outside", self.src)

    def test_each_skill_is_a_card_of_its_own(self):
        # a bordered card per skill, the description full width under the
        # name: side by side, one skill's wrapped text ran into the next
        row = self.src[self.src.index("function SkillRow("):self.src.index("function SkillsCheckPanel(")]
        self.assertIn('border: "1px solid var(--hair)"', row)
        self.assertIn("{item.description && <div style=", row)
        self.assertNotIn("minWidth: 180", row)
        self.assertNotIn("{item.description}</span>", row)
        # the folders in System > skills read as headings, not as one more line
        self.assertIn('<SectionLabel style={{ marginTop: 6 }}>{f.folder === "shared"', self.src)

    def test_no_colour_literals_and_no_em_dash(self):
        self.assertNotRegex(self.src, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotIn("oklch(", self.src)
        self.assertNotIn("rgb(", self.src)
        self.assertNotIn("—", self.src)

    def test_the_index_loads_it_in_the_package_block(self):
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="system.jsx"'), html.index('src="skills.jsx"'))
        self.assertLess(html.index('src="skills.jsx"'), html.index('src="app.jsx"'))


if __name__ == "__main__":
    unittest.main()
