"""The console on a phone, pinned by text: the page compiles its JSX in
the browser and has no build step, so these tests read styles.css and
the views the way the other static tests do.

The failure they hold off: a grid or flex child defaults to
min-width:auto, so one unbreakable line (a path, a hash, a nowrap
activity line, a wide table) widens its track past the viewport and
.main's overflow:hidden cuts the page off on the right.
"""
import pathlib
import re
import unittest

_STATIC = pathlib.Path(__file__).resolve().parents[2] / "cousin_lib" \
    / "console_static"


def _read(name):
    return (_STATIC / name).read_text(encoding="utf-8")


def _mobile_css():
    """Every rule inside a `@media (max-width: 820px)` block, joined."""
    css = _read("styles.css")
    out = []
    for m in re.finditer(r"@media \(max-width: 820px\) \{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append(css[m.end():i])
    return "\n".join(out)


class MobileTracksShrink(unittest.TestCase):
    """Single-column grids on a phone are minmax(0, 1fr), never a bare
    1fr, whose minimum is the widest child's content."""

    def setUp(self):
        self.css = _mobile_css()

    def test_no_bare_1fr_single_column_override(self):
        self.assertNotRegex(self.css, r"grid-template-columns:\s*1fr\b")

    def test_inline_grids_and_named_grids_collapse_to_a_shrinkable_track(self):
        for sel in ('div[style*="grid-template-columns"]',
                    "[data-memory-grid]", ".mt-layout"):
            self.assertIn(sel, self.css, sel)
        self.assertIn("minmax(0, 1fr)", self.css)

    def test_grid_children_may_shrink(self):
        self.assertRegex(self.css, r'\[style\*="display: grid"\] > \*')
        self.assertIn("min-width: 0", self.css)

    def test_card_stats_are_two_shrinkable_columns(self):
        self.assertIn("repeat(2, minmax(0, 1fr))", self.css)


class MobileTablesAndRows(unittest.TestCase):
    """Wide tables either scroll in their own box or stack into cards;
    header rows wrap instead of running off the right edge."""

    def setUp(self):
        self.css = _mobile_css()
        self.views = _read("views.jsx")

    def test_tracker_table_stacks_with_labelled_cells(self):
        self.assertIn("table.data.tracker-table thead { display: none; }",
                      self.css)
        for label in ("title", "owner", "domain", "state", "tags",
                      "updated", "actions"):
            self.assertIn('data-label="%s"' % label, self.views, label)

    def test_panel_bodies_scroll_sideways(self):
        self.assertRegex(self.css, r"\.panel-body[^{]*\{[^}]*overflow-x: auto")

    def test_job_header_row_wraps(self):
        self.assertIn('className="job-head"', self.views)
        self.assertIn(".job-head { flex-wrap: wrap;", self.css)

    def test_meeting_thread_header_wraps(self):
        self.assertIn(".mt-thread-hdr { flex-wrap: wrap;", self.css)

    def test_drawers_clear_the_safe_area_topbar(self):
        self.assertIn("top: calc(36px + env(safe-area-inset-top, 0px));",
                      self.css)



def _function(text, name):
    """One top-level `function name(` up to the next top-level function."""
    start = text.index("function %s(" % name)
    nxt = re.search(r"^function \w+\(", text[start + 1:], re.M)
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _squash(text):
    return re.sub(r"\s+", " ", text).strip()


def _jsx_block(text, opener, closer=")}"):
    """From `opener` to the `closer` that ends it at the opener's indent."""
    start = text.index(opener)
    line_start = text.rindex("\n", 0, start) + 1
    indent = text[line_start:start]
    end = text.index("\n" + indent + closer, start)
    return text[start:end]


# ChatHeader's desktop return and MainHeader's chat status as 2.2.0 has
# them (whitespace aside): a phone gets its own branch, the desktop keeps
# this markup to the character.
_CHAT_HEADER_DESKTOP_2_2_0 = r"""
  return (
    <div className="chat-header">
      <span className="ch-title">
        <span className="ch-sub">chat &middot; @{cousin.slug}{chatUser ? ` as ${chatUser}` : ""}</span>
        {remote && <span className="ch-sub" title={`a hive node at ${cousin.host}:${cousin.port}`}>&middot; remote</span>}
      </span>
      <span style={{ flex: 1 }} />
      {!embed && !remote && readsEffort && (
        <label className="ch-effort">
          <span>effort</span>
          <select
            value={effort}
            onChange={e => applyEffort(e.target.value)}
            disabled={!efforts.length}
            title={cousin.runner
              ? "cousin.toml [agent] effort: read by the runner at the next start"
              : "cousin.toml [runtime] effort: rendered into the agent command at the next start"}
            className="sel-inline"
            style={{ height: btnH }}
          >
            {!efforts.length && <option value={effort}>{effort || "..."}</option>}
            {efforts.map(l => <option key={l} value={l}>{l}</option>)}
          </select>
          {effortHint && (
            <span style={{ color: effortHint.startsWith("failed") ? "var(--red)" : "var(--fg-2)" }}>
              {effortHint}
            </span>
          )}
        </label>
      )}
      {!remote && (<>
      <button
        className="btn ghost"
        onClick={onArchive}
        disabled={!chatUser}
        title="archive ALL active messages"
        style={{ height: btnH }}
      >archive</button>
      <button
        className={"btn ghost" + (showArchived ? " active" : "")}
        onClick={() => setShowArchived(v => !v)}
        title={showArchived ? "showing archived messages, click to return to live" : "browse archived messages"}
        style={{ height: btnH }}
      >{showArchived ? "live" : "archived"}</button>
      </>)}
      {setMediaShown && (
        <button
          className={"btn ghost chat-media-toggle" + (mediaShown ? "" : " active")}
          onClick={() => setMediaShown(v => !v)}
          aria-pressed={!mediaShown}
          title={mediaShown ? "media shown: click to hide images, videos and audio" : "media hidden: click to show"}
          style={{ height: btnH }}
        >{mediaShown ? "media on" : "media off"}</button>
      )}
      {!remote && (
      <input
        className="chat-search"
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="search..."
        style={{ height: btnH }}
      />
      )}
      {!paneOpen && !embed && !remote && (
        <button
          className="btn ghost"
          onClick={() => setPaneOpen(true)}
          title={cousin.runner ? "show the reasoning stream beside the chat" : "show the terminal pane beside the chat"}
          style={{ height: btnH }}
        >{cousin.runner ? "reasoning" : "terminal"} &rsaquo;</button>
      )}
      {setFullscreen && !embed && (
        <button
          className="btn ghost chat-fullscreen-toggle"
          onClick={() => setFullscreen(v => !v)}
          title={fullscreen ? "exit fullscreen" : "fullscreen chat"}
          style={{ height: btnH }}
        >{fullscreen ? "⤢ exit" : "⤢"}</button>
      )}
    </div>
  );
"""

_STATUS_DESKTOP_2_2_0 = r"""
      {view === "chat" && c && !mobile && (
        <span className="hdr-meta">
          <span className={"led " + (st ? st.tone : "gray") + (st && st.pulse ? " pulse" : "")} />
          <span className={"fleet-state tone-" + (st ? st.tone : "gray")}>{st ? st.word : c.status}</span> · {c.slug}{c.model ? ` · ${c.model}` : ""} · heartbeat {c.heartbeat}s{c.chat === "down" ? " · chat server down" : ""}
        </span>
      )}
"""


class MobileChatHeader(unittest.TestCase):
    """On a phone the chat header is one row: effort, a search button
    that opens the field (with a close x), a "⋯" menu holding archive /
    archived / media, then the pane and fullscreen toggles as icons; the
    "chat · @slug as user" label moves into the menu. The branch keys on
    the same breakpoint as the stylesheet."""

    def setUp(self):
        self.header = _function(_read("chat.jsx"), "ChatHeader")
        self.row = _jsx_block(self.header, "if (mobile) {", "}")
        self.search = _jsx_block(self.header, "if (mobile && searchOpen && !remote) {", "}")
        self.menu = _jsx_block(self.row, '{menuOpen && (')
        self.css = _mobile_css()

    def test_the_hook_uses_the_stylesheets_breakpoint(self):
        ui = _read("ui.jsx")
        self.assertIn('const MOBILE_QUERY = "(max-width: 820px)";', ui)
        self.assertIn("window.matchMedia(MOBILE_QUERY)", ui)
        self.assertRegex(ui, r"Object\.assign\(window, \{[^}]*useMobileLayout")
        self.assertIn("useMobileLayout() : false) && !embed", self.header)

    def test_the_row_holds_effort_search_menu_pane_and_fullscreen_in_order(self):
        order = ["applyEffort(e.target.value)", "ch-search-open",
                 "ch-more", "ch-pane-open", "chat-fullscreen-toggle"]
        at = [self.row.index(k) for k in order]
        self.assertEqual(at, sorted(at), order)
        self.assertIn('aria-haspopup="menu"', self.row)
        self.assertIn("setSearchOpen(true)", self.row)
        # no search field and no label in the row itself
        self.assertNotIn("<input", self.row)
        self.assertNotIn("chat &middot;", self.row.replace(self.menu, ""))

    def test_the_menu_holds_archive_archived_media_and_the_label(self):
        for token in ("onClick={onArchive}", "setShowArchived(v => !v)",
                      "setMediaShown(v => !v)", 'role="menuitem"',
                      'showArchived ? "live" : "archived"',
                      'mediaShown ? "media on" : "media off"',
                      'chat &middot; @{cousin.slug}{chatUser ? ` as ${chatUser}` : ""}'):
            self.assertIn(token, self.menu, token)

    def test_the_search_opens_full_width_and_closes_clearing(self):
        self.assertIn('className="chat-search"', self.search)
        self.assertIn("setSearch(e.target.value)", self.search)
        self.assertIn("onClick={closeSearch}", self.search)
        self.assertIn('const closeSearch = () => { setSearch(""); setSearchOpen(false); };',
                      self.header)

    def test_every_desktop_action_is_reachable_on_the_phone(self):
        phone = self.row + self.search
        for action in ("applyEffort(e.target.value)", "onClick={onArchive}",
                       "setShowArchived(v => !v)", "setMediaShown(v => !v)",
                       "setSearch(e.target.value)", "setPaneOpen(true)",
                       "setFullscreen(v => !v)"):
            self.assertIn(action, _CHAT_HEADER_DESKTOP_2_2_0, action)
            self.assertIn(action, phone, action)

    def test_the_menu_closes_on_a_tap_away_and_escape(self):
        self.assertIn('document.addEventListener("pointerdown", away)', self.header)
        self.assertIn('e.key === "Escape"', self.header)

    def test_one_row_of_36px_targets(self):
        self.assertIn(".chat-header.ch-mobile { flex-wrap: nowrap !important;", self.css)
        self.assertRegex(self.css, r"\.btn\.ch-icon \{[^}]*width: 36px;[^}]*height: 36px;")
        self.assertRegex(self.css, r"ch-mobile select\.sel-inline \{ height: 36px;")
        self.assertRegex(self.css, r"ch-mobile input\.chat-search \{[^}]*height: 36px;")
        self.assertRegex(self.css, r"\.ch-menu \.btn \{[^}]*height: 40px;")


class MobileChatStatus(unittest.TestCase):
    """The status under the title is one line on a phone: dot, state,
    model cut with an ellipsis; no slug (the title), no heartbeat."""

    def setUp(self):
        self.main = _function(_read("app.jsx"), "MainHeader")
        self.line = _jsx_block(self.main, '{view === "chat" && c && mobile && (')
        self.css = _mobile_css()

    def test_dot_state_model_only(self):
        self.assertIn('className="hdr-meta hdr-meta-m"', self.line)
        self.assertIn("{st ? st.word : c.status}", self.line)
        self.assertIn("c.model", self.line)
        self.assertNotIn("c.slug", self.line)
        self.assertNotIn("heartbeat", self.line)

    def test_one_line_with_an_ellipsis(self):
        self.assertIn(".main-header .hdr-meta.hdr-meta-m { flex: 1 1 100%; "
                      "min-width: 0; white-space: nowrap; overflow: hidden; }", self.css)
        self.assertRegex(self.css, r"\.hdr-model \{[^}]*text-overflow: ellipsis;")


class DesktopChatHeaderUnchanged(unittest.TestCase):
    """The desktop keeps 2.2.0's chat header and status line exactly;
    the phone-only classes live inside the phone breakpoint only."""

    def test_chat_header_desktop_markup_is_2_2_0s(self):
        header = _function(_read("chat.jsx"), "ChatHeader")
        self.assertTrue(_squash(header).endswith(
            _squash(_CHAT_HEADER_DESKTOP_2_2_0) + " }"))

    def test_status_line_desktop_markup_is_2_2_0s(self):
        self.assertIn(_squash(_STATUS_DESKTOP_2_2_0), _squash(_read("app.jsx")))

    def test_phone_classes_are_styled_only_inside_the_breakpoint(self):
        css = _read("styles.css")
        mobile = _mobile_css()
        for name in (".ch-mobile", ".ch-menu", ".ch-icon", ".hdr-meta-m", ".ch-more-wrap"):
            self.assertIn(name, mobile, name)
            self.assertEqual(css.count(name), mobile.count(name) + _comments(css).count(name), name)


def _comments(css):
    return "\n".join(re.findall(r"/\*.*?\*/", css, re.S))


if __name__ == "__main__":
    unittest.main()
