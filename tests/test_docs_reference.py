"""The reference docs are tested from the code, so they cannot drift.

(a) Every cousin.toml [agent] key (agent_settings.SCHEMA) is documented
    in docs/configuration.md, and every key that applies without a
    restart is named so in "Config changed under a live session".
(b) The console's routes (the runtime registry, every console module
    imported) and the headings of docs/reference/console-api.md match
    both ways, a path parameter being one slot whatever its name.
(c) Every relative link and anchor in a tracked Markdown file resolves.
(d) A released CHANGELOG section is the text its own tag shipped,
    except a correction listed in tests/data/changelog_corrections.txt.
(e) Every [project.scripts] entry is in docs/commands.md's class table
    exactly once, with one of the five classes.
(f) Every page under docs/ (docs/reference/ aside) is linked from exactly
    one of the README's next-steps groups, getting-started.md first.
(g) Every page linked under "Optional" carries the marker line under its
    H1, and no page under "Start here" does.
(h) docs/getting-started.md is within its line budget and names no
    command whose class is optional, internal or developer before its
    last H2 (the section that lists what is optional).

Each check is a plain function over its inputs, so the red cases below
feed it synthetic text; the real-tree tests call the same function on
the repo. (d) needs git and the release tags: without them it skips and
prints why, since a skip is not a gate, and fails instead where
COUSIN_REQUIRE_TAGS is set (CI, which fetches the tags)."""
import importlib
import os
import pathlib
import pkgutil
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIGURATION = ROOT / "docs" / "configuration.md"
CONSOLE_API = ROOT / "docs" / "reference" / "console-api.md"
CHANGELOG = ROOT / "CHANGELOG.md"
CORRECTIONS = ROOT / "tests" / "data" / "changelog_corrections.txt"
COMMANDS = ROOT / "docs" / "commands.md"
README = ROOT / "README.md"
GETTING_STARTED = ROOT / "docs" / "getting-started.md"
PYPROJECT = ROOT / "pyproject.toml"

# A fence opens or closes only on a line that starts (up to 3 spaces in)
# with ``` or ~~~. A ```` ```mermaid ```` inside a prose line is a code
# span, not a fence: stripping ```...``` pairs across the text would eat
# everything to the next fence.
FENCE = re.compile(r"^ {0,3}(```|~~~)")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
# A code span: a run of backticks closed by a run of the same length.
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def git(*args):
    """git's stdout in this repo, or None when git or the object is missing."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT)] + list(args),
                             capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out.stdout.decode("utf-8")


def prose(text):
    """`text` with every fenced line blanked, line numbers kept."""
    out, fenced = [], False
    for line in text.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
            out.append("")
        else:
            out.append("" if fenced else line)
    return "\n".join(out)


def _section(text, heading):
    """The lines under `heading` (a full heading line) up to the next
    heading of the same or a higher level; "" when it is absent."""
    lines = text.split("\n")
    level = len(heading) - len(heading.lstrip("#"))
    for i, line in enumerate(lines):
        if line.strip() == heading:
            body = []
            for rest in lines[i + 1:]:
                m = HEADING.match(rest)
                if m and len(m.group(1)) <= level:
                    break
                body.append(rest)
            return "\n".join(body)
    return ""


# -- (a) [agent] keys in docs/configuration.md ------------------------------

LIVE_CHANGE = "### Config changed under a live session"
NO_RESTART = "without a restart"


def key_forms(key):
    return ("`%s`" % key, "`[agent] %s`" % key, "[agent.%s]" % key)


def documents(text, key):
    return any(form in text for form in key_forms(key))


def undocumented_keys(schema, doc):
    """The schema keys configuration.md does not name, outside fences."""
    text = prose(doc)
    return [key for key in schema if not documents(text, key)]


def live_keys_unnamed(schema, doc):
    """The keys with "restart": False that the live-change section does
    not name in the bullet or paragraph saying they apply without a
    restart."""
    section = prose(_section(doc, LIVE_CHANGE))
    blocks, current = [], []
    for line in section.split("\n"):
        if not line.strip() or line.startswith("- "):
            if current:
                blocks.append("\n".join(current))
            current = [line] if line.strip() else []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    saying = [b for b in blocks if NO_RESTART in re.sub(r"\s+", " ", b)]
    return [key for key, spec in schema.items()
            if spec.get("restart", True) is False
            and not any(documents(b, key) for b in saying)]


# -- (b) console routes vs docs/reference/console-api.md ---------------------

OLDER_ROUTES = "## Routes the older console had"
ROUTE_HEADING = re.compile(r"^#{2,4} `([A-Z]+) (/[^`\s]*)`", re.M)
PARAM = re.compile(r"\{[^}/]+\}|<[^>/]+>")
# Routes served outside the router, each with the module that serves it
# and how to ask that module whether it still does.
NOT_IN_ROUTER = {
    ("POST", "/peer/send"): ("cousin_lib.console.peer_routes", "is_peer_path"),
}


def code_routes():
    """(method, path) of every route on the registry, after importing every
    module in cousin_lib.console and calling its register(), so a registry
    another test cleared is whole again."""
    import cousin_lib.console as package
    from cousin_lib.console import router
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module("cousin_lib.console." + info.name)
        register = getattr(module, "register", None)
        if callable(register):
            register()
    return set(router.routes())


def doc_routes(doc):
    """(method, path) of the documented routes: `METHOD /path` headings
    outside fences, before the section that lists removed routes."""
    text = prose(doc).split("\n" + OLDER_ROUTES + "\n")[0]
    return set(ROUTE_HEADING.findall(text))


def normalise(path):
    return PARAM.sub("{}", path)


def route_drift(code, docs, exceptions=()):
    """(in code not in docs, in docs not in code), each sorted, parameters
    normalised; `exceptions` are documented routes served outside the
    router."""
    c = {(m, normalise(p)) for m, p in code}
    d = {(m, normalise(p)) for m, p in docs}
    allowed = {(m, normalise(p)) for m, p in exceptions}
    return sorted(c - d), sorted(d - c - allowed)


def stale_exceptions(exceptions):
    """The listed routes whose module no longer serves their path."""
    stale = []
    for (method, path), (module, predicate) in exceptions.items():
        try:
            serves = getattr(importlib.import_module(module), predicate)(path)
        except (ImportError, AttributeError):
            serves = False
        if not serves:
            stale.append("%s %s (%s.%s)" % (method, path, module, predicate))
    return stale


# -- (c) relative links and anchors ------------------------------------------

def slug(heading):
    """GitHub's anchor for a heading's text."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text):
    """The anchors a Markdown file's headings create; headings inside
    fences create none; a repeated heading gets -1, -2 ..."""
    seen, out = {}, set()
    for line in prose(text).split("\n"):
        m = HEADING.match(line)
        if not m:
            continue
        base = slug(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else "%s-%d" % (base, n))
    return out


def links(text):
    """(line number, target) of every link outside fences and code spans."""
    for number, line in enumerate(prose(text).split("\n"), 1):
        line = CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        for m in LINK.finditer(line):
            yield number, m.group(1)


def broken_links(files):
    """`files` maps a repo-relative path to its text (None: the file exists
    but is not Markdown). Every relative link in a .md file must name a
    file that exists, and an anchor on a .md target a heading there.
    Returns "path:line target: file|anchor" lines."""
    cache, bad = {}, []

    def heads(path):
        if path not in cache:
            cache[path] = anchors(files[path] or "")
        return cache[path]

    for path, text in sorted(files.items()):
        if not path.endswith(".md") or text is None:
            continue
        for number, target in links(text):
            if not target or SCHEME.match(target):
                continue
            file_part, _, frag = target.partition("#")
            if file_part:
                resolved = os.path.normpath(os.path.join(os.path.dirname(path), file_part))
            else:
                resolved = path
            where = "%s:%d %s" % (path, number, target)
            if resolved not in files and not any(f.startswith(resolved.rstrip("/") + "/")
                                                 for f in files):
                bad.append(where + ": file")
            elif frag and resolved.endswith(".md") and resolved in files \
                    and frag not in heads(resolved):
                bad.append(where + ": anchor")
    return bad


def repo_files():
    """Every git-visible file (tracked, or untracked and not ignored), the
    Markdown ones with their text; without git, the tree under ROOT."""
    out = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if out is not None:
        names = sorted(set(n for n in out.split("\0") if n))
    else:
        names = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                       if p.is_file() and ".git" not in p.parts)
    return {n: ((ROOT / n).read_text(encoding="utf-8") if n.endswith(".md") else None)
            for n in names if (ROOT / n).is_file()}


# -- (d) released CHANGELOG sections -----------------------------------------

SECTION = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\b.*$", re.M)


def sections(text):
    """{(major, minor, patch): the section's text}, a section being its
    `## X.Y.Z` line up to the next one, trailing blank lines dropped."""
    found = list(SECTION.finditer(text))
    out = {}
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        out[tuple(int(g) for g in m.groups())] = text[m.start():end].rstrip()
    return out


def dotted(version):
    return "%d.%d.%d" % version


def changelog_drift(head, at_tag, corrections=None):
    """`head`: CHANGELOG.md now. `at_tag`: {version: CHANGELOG.md at that
    version's tag}. `corrections`: {version: the section as a sanctioned
    correction left it}. A section is compared with the CHANGELOG at its
    own tag; a section older than the oldest tag with the oldest tag's; a
    section with no tag is not released yet and is not compared. Returns
    one line per drifted, missing or added section."""
    corrections = corrections or {}
    if not at_tag:
        return []
    oldest = min(at_tag)
    now = sections(head)
    shipped = {v: sections(text) for v, text in at_tag.items()}

    def base(version):
        if version in at_tag:
            return version
        return oldest if version < oldest else None

    bad = []
    released = {v for v in now if base(v) is not None}
    for tag, secs in shipped.items():
        released |= {v for v in secs if base(v) == tag}
    for version in sorted(released, reverse=True):
        tag = base(version)
        then = shipped[tag].get(version)
        name, label = dotted(version), "v" + dotted(tag)
        if version not in now:
            bad.append("%s: in %s, missing now" % (name, label))
        elif then is None:
            bad.append("%s: not in %s, added after it shipped" % (name, label))
        elif now[version] != then and corrections.get(version) != now[version]:
            listed = " (listed, but edited again since)" if version in corrections else ""
            bad.append("%s: differs from %s%s" % (name, label, listed))
    return bad


def read_corrections(text):
    """[(version, commit, reason)] from the corrections file: one line per
    corrected version, `X.Y.Z <commit> <reason>`; # comments."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        version, commit, reason = line.split(None, 2)
        out.append((tuple(int(p) for p in version.split(".")), commit, reason))
    return out


def tag_versions():
    """{version: tag name} of the vX.Y.Z tags this checkout contains, or
    None without git. A tag the checkout does not reach is left out: CI on
    an older tag, run after a newer one was pushed, must not be held to the
    newer tag's sections."""
    out = git("tag", "--list", "--merged", "HEAD", "v*")
    if out is None:
        return None
    found = {}
    for name in out.split():
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", name)
        if m:
            found[tuple(int(g) for g in m.groups())] = name
    return found


# -- (e) a class for every command in docs/commands.md -----------------------

CLASS_TABLE = "## Which commands you need"
CLASSES = ("core", "optional", "cousin", "internal", "developer")
CLASS_ROW = re.compile(r"^\|\s*`(cousin-[a-z0-9-]+)`\s*\|\s*([^|]*?)\s*\|")


def command_rows(doc):
    """[(command, class)] of the class table's rows, in order."""
    rows = []
    for line in prose(_section(doc, CLASS_TABLE)).split("\n"):
        m = CLASS_ROW.match(line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def class_table_problems(scripts, rows):
    """One line per script missing from the table or in it more than once,
    per row with a class that is not one of CLASSES, and per row naming
    no installed script."""
    bad, seen = [], {}
    for command, cls in rows:
        seen.setdefault(command, []).append(cls)
        if cls not in CLASSES:
            bad.append("%s: class %r is not one of %s" % (command, cls, ", ".join(CLASSES)))
    for script in sorted(scripts):
        found = seen.get(script, [])
        if not found:
            bad.append("%s: not in the class table" % script)
        elif len(found) > 1:
            bad.append("%s: in the class table %d times (%s)"
                       % (script, len(found), ", ".join(found)))
    for command in sorted(set(seen) - set(scripts)):
        bad.append("%s: in the class table, not an installed script" % command)
    return bad


def installed_scripts():
    import tomllib
    return set(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"])


# -- (f) and (g) the README's groups, and the optional marker -----------------

NEXT_STEPS = "## Where to go next"
GROUPS = ("Start here", "Optional", "Reference")
OPTIONAL_MARKER = "*Optional: nothing on this page is needed to run a cousin.*"


def readme_groups(readme):
    """{group: [repo-relative .md path, ...]} for each `### <group>` under
    the next-steps section, in link order; a link's anchor is dropped and
    a directory link is left out."""
    section = _section(readme, NEXT_STEPS)
    out = {}
    for group in GROUPS:
        body = _section(section, "### " + group)
        paths = []
        for _number, target in links(body):
            path = target.partition("#")[0]
            if path.endswith(".md") and not SCHEME.match(path):
                paths.append(os.path.normpath(path))
        out[group] = paths
    return out


def grouping_problems(pages, groups, first="docs/getting-started.md"):
    """`pages`: the docs/*.md pages (repo-relative). One line per page in
    no group or in more than one, per group missing, and when `first` is
    not the first link under "Start here"."""
    bad = []
    for group in GROUPS:
        if not groups.get(group):
            bad.append("group %r: missing or empty" % group)
    for page in sorted(pages):
        found = [g for g in GROUPS if page in groups.get(g, [])]
        if not found:
            bad.append("%s: in no README group" % page)
        elif len(found) > 1:
            bad.append("%s: in %d README groups (%s)" % (page, len(found), ", ".join(found)))
    start = groups.get("Start here") or [None]
    if start[0] != first:
        bad.append("%s: not the first link under Start here (%s is)" % (first, start[0]))
    return bad


def under_h1(text):
    """The first non-blank line after the page's H1, or None."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            for rest in lines[i + 1:]:
                if rest.strip():
                    return rest.strip()
            return None
    return None


def marker_problems(groups, texts):
    """`texts`: {path: page text}. One line per Optional page without the
    marker under its H1, and per Start here page with it."""
    bad = []
    for page in groups.get("Optional", []):
        if under_h1(texts[page]) != OPTIONAL_MARKER:
            bad.append("%s: listed as Optional, no marker under its H1" % page)
    for page in groups.get("Start here", []):
        if under_h1(texts[page]) == OPTIONAL_MARKER:
            bad.append("%s: listed under Start here, marked optional" % page)
    return bad


# -- (h) docs/getting-started.md ----------------------------------------------

GETTING_STARTED_BUDGET = 350
COMMAND_NAME = re.compile(r"(?<![\w-])cousin-[a-z0-9]+(?:-[a-z0-9]+)*")


def getting_started_problems(text, classes, budget=GETTING_STARTED_BUDGET):
    """`classes`: {command: class}. One line when the page is over budget,
    and one per non-core command named before the last H2 (code blocks
    included: a command in an example is a command to run). A name that
    is not a command (a unit, a file) is not checked."""
    bad = []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    if len(lines) > budget:
        bad.append("%d lines, over the budget of %d" % (len(lines), budget))
    h2 = [i for i, line in enumerate(prose(text).split("\n")) if line.startswith("## ")]
    end = h2[-1] if h2 else len(lines)
    for number, line in enumerate(lines[:end], 1):
        for name in COMMAND_NAME.findall(line):
            cls = classes.get(name)
            if cls is not None and cls != "core":
                bad.append("line %d: %s is %s, not core; name it in the last section"
                           % (number, name, cls))
    return bad


# -- the real tree -------------------------------------------------------------

class AgentKeysDocumented(unittest.TestCase):
    def setUp(self):
        from cousin_lib.agent_settings import SCHEMA
        self.schema = SCHEMA
        self.doc = CONFIGURATION.read_text(encoding="utf-8")

    def test_every_agent_key_is_in_configuration_md(self):
        self.assertEqual(undocumented_keys(self.schema, self.doc), [],
                         "document these [agent] keys in docs/configuration.md")

    def test_the_keys_that_apply_live_are_named_in_the_live_change_section(self):
        self.assertTrue([k for k, s in self.schema.items() if s.get("restart", True) is False],
                        "no key applies without a restart: the check would test nothing")
        self.assertEqual(live_keys_unnamed(self.schema, self.doc), [],
                         "name these keys in %r as applying %s" % (LIVE_CHANGE, NO_RESTART))


class ConsoleRoutesDocumented(unittest.TestCase):
    def test_routes_and_docs_match_both_ways(self):
        code = code_routes()
        docs = doc_routes(CONSOLE_API.read_text(encoding="utf-8"))
        self.assertGreater(len(code), 100, "the registry looks empty")
        in_code, in_docs = route_drift(code, docs, NOT_IN_ROUTER)
        self.assertEqual(in_code, [], "in code, not in docs/reference/console-api.md")
        self.assertEqual(in_docs, [], "in docs/reference/console-api.md, not in code")

    def test_each_route_served_outside_the_router_is_still_served(self):
        self.assertEqual(stale_exceptions(NOT_IN_ROUTER), [])


class LinksResolve(unittest.TestCase):
    def test_every_relative_link_and_anchor_resolves(self):
        files = repo_files()
        self.assertIn("README.md", files)
        self.assertEqual(broken_links(files), [])


class ReleasedChangelogUnchanged(unittest.TestCase):
    def test_each_released_section_is_what_its_tag_shipped(self):
        tags = tag_versions()
        reason = None
        if tags is None:
            reason = "git is not available here"
        elif not tags:
            reason = "no vX.Y.Z tags in this clone (CI fetches them: fetch-depth: 0)"
        if reason:
            # CI sets COUSIN_REQUIRE_TAGS: there a missing tag is a failure,
            # never a skip that reads as a pass.
            if os.environ.get("COUSIN_REQUIRE_TAGS"):
                self.fail("(d) cannot run: %s" % reason)
            print("\n  skip (d): %s; released CHANGELOG sections were not compared"
                  % reason, flush=True)
            self.skipTest("(d) not run: %s" % reason)
        at_tag = {}
        for version, name in tags.items():
            text = git("show", "%s:CHANGELOG.md" % name)
            if text is not None:
                at_tag[version] = text
        corrections = {}
        for version, commit, _reason in read_corrections(
                CORRECTIONS.read_text(encoding="utf-8")):
            text = git("show", "%s:CHANGELOG.md" % commit)
            self.assertIsNotNone(text, "correction %s: commit %s is not in this clone"
                                 % (dotted(version), commit))
            corrections[version] = sections(text).get(version)
        head = CHANGELOG.read_text(encoding="utf-8")
        self.assertEqual(changelog_drift(head, at_tag, corrections), [],
                         "a released section changed: revert it, or list the "
                         "correction in tests/data/changelog_corrections.txt")


class CommandClasses(unittest.TestCase):
    def test_every_script_has_one_class(self):
        scripts = installed_scripts()
        rows = command_rows(COMMANDS.read_text(encoding="utf-8"))
        self.assertGreater(len(scripts), 30, "pyproject's scripts look empty")
        self.assertEqual(class_table_problems(scripts, rows), [],
                         "fix the table under %r in docs/commands.md" % CLASS_TABLE)


class ReadmeGroups(unittest.TestCase):
    def setUp(self):
        self.groups = readme_groups(README.read_text(encoding="utf-8"))

    def test_every_page_is_in_exactly_one_group(self):
        pages = ["docs/" + p.name for p in sorted((ROOT / "docs").glob("*.md"))]
        self.assertIn("docs/getting-started.md", pages)
        self.assertEqual(grouping_problems(pages, self.groups), [])

    def test_optional_pages_carry_the_marker_and_start_here_pages_do_not(self):
        texts = {p: (ROOT / p).read_text(encoding="utf-8")
                 for g in GROUPS for p in self.groups[g]}
        self.assertEqual(marker_problems(self.groups, texts), [])


class GettingStarted(unittest.TestCase):
    def test_within_budget_and_core_commands_only_before_the_optional_section(self):
        classes = dict(command_rows(COMMANDS.read_text(encoding="utf-8")))
        self.assertIn("cousin-runner", classes)
        text = GETTING_STARTED.read_text(encoding="utf-8")
        self.assertEqual(getting_started_problems(text, classes), [])


# -- the red cases ---------------------------------------------------------------

class AgentKeysRed(unittest.TestCase):
    SCHEMA = {"model": {}, "dreaming": {"restart": False}}
    DOC = ("Set `model` in the [agent] table.\n\n"
           "### Config changed under a live session\n\n"
           "- **`cousin.toml`.** Read at start. Keys that apply without a\n"
           "  restart (`[agent] dreaming`): the note names them.\n\n"
           "### Next\n")

    def test_the_synthetic_doc_is_green(self):
        self.assertEqual(undocumented_keys(self.SCHEMA, self.DOC), [])
        self.assertEqual(live_keys_unnamed(self.SCHEMA, self.DOC), [])

    def test_each_key_form_documents_a_key(self):
        for doc in ("`sessions`", "`[agent] sessions`", "### [agent.sessions]"):
            self.assertEqual(undocumented_keys({"sessions": {}}, doc), [], doc)

    def test_an_undocumented_key_fails_by_name(self):
        schema = dict(self.SCHEMA, wren_mode={})
        self.assertEqual(undocumented_keys(schema, self.DOC), ["wren_mode"])

    def test_a_live_key_named_only_outside_the_live_change_section_fails(self):
        schema = dict(self.SCHEMA, kestrel_at={"restart": False})
        doc = "`kestrel_at` is the hour.\n\n" + self.DOC
        self.assertEqual(undocumented_keys(schema, doc), [])
        self.assertEqual(live_keys_unnamed(schema, doc), ["kestrel_at"])

    def test_a_key_only_inside_a_fence_fails(self):
        schema = dict(self.SCHEMA, wren_mode={})
        doc = self.DOC + "\n```toml\n[agent]\n# `wren_mode`\nwren_mode = true\n```\n"
        self.assertEqual(undocumented_keys(schema, doc), ["wren_mode"])


class ConsoleRoutesRed(unittest.TestCase):
    CODE = {("GET", "/api/jobs/{job_id}"), ("POST", "/api/auth/login")}
    DOC = ("# Console API\n\n### `GET /api/jobs/<id>`\n\n#### `POST /api/auth/login`\n\n"
           "## Routes the older console had\n\n### `POST /api/testa/old`\n")

    def test_a_parameter_is_one_slot_whatever_its_name(self):
        self.assertEqual(route_drift(self.CODE, doc_routes(self.DOC)), ([], []))

    def test_a_route_in_code_only_fails(self):
        code = self.CODE | {("GET", "/api/wren/{slug}")}
        in_code, in_docs = route_drift(code, doc_routes(self.DOC))
        self.assertEqual(in_code, [("GET", "/api/wren/{}")])
        self.assertEqual(in_docs, [])

    def test_a_heading_with_no_route_fails(self):
        doc = self.DOC.replace("## Routes", "### `POST /api/kestrel`\n\n## Routes")
        self.assertEqual(route_drift(self.CODE, doc_routes(doc)),
                         ([], [("POST", "/api/kestrel")]))

    def test_the_older_console_section_is_ignored(self):
        self.assertNotIn(("POST", "/api/testa/old"), doc_routes(self.DOC))

    def test_a_listed_exception_passes_only_while_listed(self):
        doc = self.DOC.replace("## Routes", "### `POST /peer/send`\n\n## Routes")
        self.assertEqual(route_drift(self.CODE, doc_routes(doc), NOT_IN_ROUTER), ([], []))
        self.assertEqual(route_drift(self.CODE, doc_routes(doc))[1], [("POST", "/peer/send")])

    def test_an_exception_its_module_no_longer_serves_fails(self):
        gone = {("POST", "/api/sam/send"): ("cousin_lib.console.peer_routes", "is_peer_path")}
        self.assertEqual(stale_exceptions(gone),
                         ["POST /api/sam/send (cousin_lib.console.peer_routes.is_peer_path)"])
        missing = {("POST", "/peer/send"): ("cousin_lib.console.peer_routes", "no_such_check")}
        self.assertEqual(len(stale_exceptions(missing)), 1)


class LinksRed(unittest.TestCase):
    TARGET = "# Console\n\n## The chat view\n\n```\n## Only in a fence\n```\n"

    def check(self, text, **extra):
        files = {"docs/console.md": self.TARGET, "docs/page.md": text}
        files.update(extra)
        return broken_links(files)

    def test_a_good_link_and_anchor_pass(self):
        self.assertEqual(self.check("[x](console.md#the-chat-view) [y](console.md)"), [])

    def test_a_missing_file_fails(self):
        self.assertEqual(self.check("See [x](missing.md)."), ["docs/page.md:1 missing.md: file"])

    def test_a_missing_anchor_fails(self):
        self.assertEqual(self.check("[x](console.md#no-such-heading)"),
                         ["docs/page.md:1 console.md#no-such-heading: anchor"])

    def test_a_heading_only_inside_a_fence_creates_no_anchor(self):
        self.assertEqual(self.check("[x](console.md#only-in-a-fence)"),
                         ["docs/page.md:1 console.md#only-in-a-fence: anchor"])

    def test_an_inline_mermaid_fence_in_prose_hides_no_heading(self):
        target = ("# Console\n\nMessages render as ```` ```mermaid ```` blocks.\n\n"
                  "## Later heading\n\n```\ncode\n```\n")
        self.assertEqual(self.check("[x](console.md#later-heading)",
                                    **{"docs/console.md": target}), [])

    def test_a_same_file_anchor_resolves_in_that_file(self):
        self.assertEqual(self.check("## Same file heading\n\n[x](#same-file-heading)"), [])
        self.assertEqual(self.check("[x](#no-heading-here)"),
                         ["docs/page.md:1 #no-heading-here: anchor"])

    def test_links_in_code_and_urls_are_not_checked(self):
        text = ("`[x](missing.md)` and [y](https://example.invalid/missing.md)\n"
                "```\n[z](missing.md)\n```\n[m](mailto:ana@example.invalid)\n")
        self.assertEqual(self.check(text), [])

    def test_slugs_follow_github(self):
        self.assertEqual(slug("`GET /api/jobs/<id>`"), "get-apijobsid")
        self.assertEqual(slug("An install that runs the two units: move to the supervisor"),
                         "an-install-that-runs-the-two-units-move-to-the-supervisor")
        self.assertEqual(anchors("## Notes\n\n## Notes\n\n## Notes\n"),
                         {"notes", "notes-1", "notes-2"})

    def test_an_anchor_on_a_non_markdown_target_checks_the_file_only(self):
        self.assertEqual(self.check("[x](../tests/run.py#L3)", **{"tests/run.py": None}), [])
        self.assertEqual(self.check("[x](../tests/gone.py#L3)"),
                         ["docs/page.md:1 ../tests/gone.py#L3: file"])


class ChangelogRed(unittest.TestCase):
    HEAD = ("# Changelog\n\n## 1.2.0 - 2026-10-03\n\n- Toki's new thing.\n\n"
            "## 1.1.0 - 2026-10-02\n\n- Priya's fix.\n\n"
            "## 1.0.0 - 2026-10-01\n\n- First.\n\n## 0.9.0 - 2026-09-30\n\n- Before.\n")
    AT_100 = ("# Changelog\n\n## 1.0.0 - 2026-10-01\n\n- First.\n\n"
              "## 0.9.0 - 2026-09-30\n\n- Before.\n")
    AT_110 = ("# Changelog\n\n## 1.1.0 - 2026-10-02\n\n- Priya's fix.\n\n" + AT_100[13:])

    def tags(self):
        return {(1, 0, 0): self.AT_100, (1, 1, 0): self.AT_110}

    def test_the_synthetic_head_is_green_and_the_untagged_section_is_not_compared(self):
        self.assertEqual(changelog_drift(self.HEAD, self.tags()), [])
        head = self.HEAD.replace("Toki's new thing.", "Toki's new thing, reworded.")
        self.assertEqual(changelog_drift(head, self.tags()), [])

    def test_an_extra_bullet_in_a_tagged_section_fails_by_version(self):
        head = self.HEAD.replace("- Priya's fix.\n", "- Priya's fix.\n- Sam's slip.\n")
        self.assertEqual(changelog_drift(head, self.tags()), ["1.1.0: differs from v1.1.0"])

    def test_a_section_older_than_the_oldest_tag_compares_with_it(self):
        head = self.HEAD.replace("- Before.", "- Before, rewritten.")
        self.assertEqual(changelog_drift(head, self.tags()), ["0.9.0: differs from v1.0.0"])

    def test_a_deleted_tagged_section_fails(self):
        head = self.HEAD.replace("## 1.1.0 - 2026-10-02\n\n- Priya's fix.\n\n", "")
        self.assertEqual(changelog_drift(head, self.tags()), ["1.1.0: in v1.1.0, missing now"])

    def test_a_listed_correction_passes_as_it_was_left(self):
        head = self.HEAD.replace("- Priya's fix.\n", "- Priya's fix, in the right module.\n")
        corrected = sections(head)[(1, 1, 0)]
        self.assertEqual(changelog_drift(head, self.tags(), {(1, 1, 0): corrected}), [])

    def test_a_listed_correction_edited_again_fails(self):
        head = self.HEAD.replace("- Priya's fix.\n", "- Priya's fix, in the right module.\n")
        corrected = sections(head)[(1, 1, 0)]
        again = head.replace("right module.", "right module, and Mallory's line.")
        self.assertEqual(changelog_drift(again, self.tags(), {(1, 1, 0): corrected}),
                         ["1.1.0: differs from v1.1.0 (listed, but edited again since)"])

    def test_the_corrections_file_parses(self):
        rows = read_corrections(CORRECTIONS.read_text(encoding="utf-8"))
        self.assertTrue(rows)
        for version, commit, reason in rows:
            self.assertEqual(len(version), 3)
            self.assertRegex(commit, r"^[0-9a-f]{7,40}$")
            self.assertTrue(reason.strip())


class CommandClassesRed(unittest.TestCase):
    DOC = ("# Commands\n\n## Which commands you need\n\n"
           "| command | class | what |\n|---|---|---|\n"
           "| `cousin-spawn` | core | make one |\n"
           "| `cousin-runner` | internal | drives one |\n\n"
           "## Running cousins\n\n| `cousin-wren` | core | outside the table |\n")
    SCRIPTS = {"cousin-spawn", "cousin-runner"}

    def test_the_synthetic_table_is_green(self):
        rows = command_rows(self.DOC)
        self.assertEqual(rows, [("cousin-spawn", "core"), ("cousin-runner", "internal")])
        self.assertEqual(class_table_problems(self.SCRIPTS, rows), [])

    def test_a_script_missing_from_the_table_fails(self):
        rows = command_rows(self.DOC)
        self.assertEqual(class_table_problems(self.SCRIPTS | {"cousin-wren"}, rows),
                         ["cousin-wren: not in the class table"])

    def test_a_script_with_two_classes_fails(self):
        doc = self.DOC.replace("\n\n## Running", "\n| `cousin-spawn` | cousin | again |\n\n## Running")
        self.assertEqual(class_table_problems(self.SCRIPTS, command_rows(doc)),
                         ["cousin-spawn: in the class table 2 times (core, cousin)"])

    def test_an_unknown_class_and_a_row_for_no_script_fail(self):
        doc = self.DOC.replace("| internal |", "| plumbing |")
        self.assertEqual(class_table_problems(self.SCRIPTS, command_rows(doc)),
                         ["cousin-runner: class 'plumbing' is not one of "
                          "core, optional, cousin, internal, developer"])
        self.assertEqual(class_table_problems({"cousin-spawn"}, command_rows(self.DOC)),
                         ["cousin-runner: in the class table, not an installed script"])


class ReadmeGroupsRed(unittest.TestCase):
    README = ("# x\n\n## Where to go next\n\n### Start here\n\n"
              "- [Getting started](docs/getting-started.md)\n- [Chat](docs/chat.md#replying)\n\n"
              "### Optional\n\n- [Telegram](docs/telegram.md)\n\n"
              "### Reference\n\n- [Commands](docs/commands.md)\n- [ref](docs/reference/)\n\n"
              "## License\n\n[Chat](docs/chat.md)\n")
    PAGES = ["docs/getting-started.md", "docs/chat.md", "docs/telegram.md", "docs/commands.md"]
    MARKED = "# Telegram\n\n" + OPTIONAL_MARKER + "\n\nBody.\n"

    def texts(self):
        out = {p: "# Page\n\nBody.\n" for p in self.PAGES}
        out["docs/telegram.md"] = self.MARKED
        return out

    def test_the_synthetic_readme_is_green(self):
        groups = readme_groups(self.README)
        self.assertEqual(groups["Start here"], ["docs/getting-started.md", "docs/chat.md"])
        self.assertEqual(grouping_problems(self.PAGES, groups), [])
        self.assertEqual(marker_problems(groups, self.texts()), [])

    def test_a_page_in_no_group_fails(self):
        groups = readme_groups(self.README)
        self.assertEqual(grouping_problems(self.PAGES + ["docs/meetings.md"], groups),
                         ["docs/meetings.md: in no README group"])

    def test_a_page_in_two_groups_fails(self):
        readme = self.README.replace("- [Telegram](docs/telegram.md)\n",
                                     "- [Telegram](docs/telegram.md)\n- [Chat](docs/chat.md)\n")
        self.assertEqual(grouping_problems(self.PAGES, readme_groups(readme)),
                         ["docs/chat.md: in 2 README groups (Start here, Optional)"])

    def test_getting_started_must_come_first(self):
        readme = self.README.replace(
            "- [Getting started](docs/getting-started.md)\n- [Chat](docs/chat.md#replying)\n",
            "- [Chat](docs/chat.md#replying)\n- [Getting started](docs/getting-started.md)\n")
        self.assertEqual(grouping_problems(self.PAGES, readme_groups(readme)),
                         ["docs/getting-started.md: not the first link under Start here "
                          "(docs/chat.md is)"])

    def test_an_optional_page_without_the_marker_fails(self):
        texts = self.texts()
        texts["docs/telegram.md"] = "# Telegram\n\nBody.\n\n" + OPTIONAL_MARKER + "\n"
        self.assertEqual(marker_problems(readme_groups(self.README), texts),
                         ["docs/telegram.md: listed as Optional, no marker under its H1"])

    def test_a_start_here_page_with_the_marker_fails_but_a_marked_section_passes(self):
        texts = self.texts()
        texts["docs/chat.md"] = self.MARKED.replace("Telegram", "Chat")
        self.assertEqual(marker_problems(readme_groups(self.README), texts),
                         ["docs/chat.md: listed under Start here, marked optional"])
        texts["docs/chat.md"] = "# Chat\n\nCore.\n\n## Extras\n\n" + OPTIONAL_MARKER + "\n"
        self.assertEqual(marker_problems(readme_groups(self.README), texts), [])


class GettingStartedRed(unittest.TestCase):
    CLASSES = {"cousin-health": "core", "cousin-runner": "internal",
               "cousin-telegram": "optional", "cousin-spawn": "core",
               "cousin-spawn-node": "optional"}
    PAGE = ("# Getting started\n\n## Healthy\n\nRun `cousin-health`; see\n"
            "`cousin-supervisor.service` and cousins-framework.\n\n```\ncousin-spawn wren\n```\n\n"
            "## What is optional\n\n- `cousin-telegram`, `cousin-spawn-node`, `cousin-runner`\n")

    def test_core_commands_before_and_any_command_in_the_last_section_pass(self):
        self.assertEqual(getting_started_problems(self.PAGE, self.CLASSES), [])

    def test_an_internal_command_outside_the_last_section_fails(self):
        page = self.PAGE.replace("Run `cousin-health`", "Run `cousin-runner`")
        self.assertEqual(getting_started_problems(page, self.CLASSES),
                         ["line 5: cousin-runner is internal, not core; "
                          "name it in the last section"])

    def test_the_longest_name_counts_and_a_code_block_is_checked(self):
        page = self.PAGE.replace("cousin-spawn wren", "cousin-spawn-node wren")
        self.assertEqual(getting_started_problems(page, self.CLASSES),
                         ["line 9: cousin-spawn-node is optional, not core; "
                          "name it in the last section"])

    def test_a_page_over_budget_fails(self):
        self.assertEqual(getting_started_problems(self.PAGE, self.CLASSES, budget=5),
                         ["14 lines, over the budget of 5"])


if __name__ == "__main__":
    unittest.main()
