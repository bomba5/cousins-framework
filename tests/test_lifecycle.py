"""Reincarnate and transplant: identity surgery with memory continuity.

Both operations run against a real framework root under a temp dir
with two registered cousins. The flip is injected (`do_flip`) so the
suite asserts the ORDER and the bookkeeping - snapshot, bequest,
rewrite, flip, audit - without respawning anything; one test wires
the real flip through the fake tmux executable to prove the seam
actually reaches it. The bequest prompt goes to a loopback capture
server standing in for the cousin's chat server.
"""
import contextlib
import http.server
import io
import json
import os
import pathlib
import stat
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

from tests._hermetic import HermeticCase

from cousin_lib import lifecycle
from cousin_lib.lifecycle import (braid_memory, reincarnate,
                                  reincarnate_main, rewrite_role,
                                  transplant, transplant_main)
from tests.server.test_injection import _FAKE_TMUX


class _Capture(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Capture.received.append({
            "path": self.path,
            "payload": json.loads(self.rfile.read(length)),
        })
        body = json.dumps({"ok": True, "id": 7}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        _Capture.received = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text("1. Be authored.\n")
        (self.root / "config" / "agent-cmd").write_text(
            "my-agent --sid {session_id}\n")
        self.a = self._cousin("testa", "Testa", "keeper of the ledger")
        self.b = self._cousin("testb", "Testb", "reader of the weather")
        self.flips = []
        self.tmux = self.root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.log = self.root / "tmux-calls.log"
        self.pane = self.root / "pane.txt"
        self.pane.write_text("working\n")
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(self.pane),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        from tests._fakes import agent_on_path
        agent_on_path(self, self.root)

    def _cousin(self, slug, name, role):
        home = self.root / "cousins" / slug
        (home / "data").mkdir(parents=True)
        (home / "memory" / "raw").mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\nrole = "%s"\n\n'
            '[chat]\nport = %d\ntmux_session = "%s"\n'
            % (slug, name, role, self.port, slug))
        (home / "CLAUDE.md").write_text(
            "# %s - %s\n\n## Identity\n\nYou are %s.\n\n## Voice\n\n"
            "Plain.\n" % (name, role, name))
        (home / "MEMORY.md").write_text(
            "# %s - memory index\n- %s remembers the first day\n"
            % (name, name))
        (home / "STATUS.md").write_text(
            "# %s - STATUS\n\n## Open loops\n\n- ship it\n" % name)
        (home / "self-portrait.md").write_text(
            "# %s self-portrait\nportrait of %s\n" % (name, name))
        (home / "memory" / ("%s-fact.md" % slug)).write_text(
            "%s knows a fact\n" % name)
        (home / "memory" / "raw" / "2026-09-01.jsonl").write_text(
            json.dumps({"who": slug, "text": "%s day one" % slug}) + "\n")
        (home / "memory" / "raw" / ("%s-only.jsonl" % slug)).write_text(
            json.dumps({"who": slug, "text": "%s private" % slug}) + "\n")
        return home

    def _fake_flip(self, slug):
        self.flips.append(slug)
        return {"slug": slug, "ok": True, "stages": []}

    def _audit(self):
        path = self.root / "data" / "lifecycle" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text().splitlines() if line.strip()]

    def _reincarnate(self, slug="testa", **kw):
        kw.setdefault("do_flip", self._fake_flip)
        kw.setdefault("timeout", 0.5)
        return reincarnate(slug, new_role="mender of the fence",
                           root=self.root, **kw)

    def _transplant(self, mode, **kw):
        kw.setdefault("do_flip", self._fake_flip)
        return transplant(donor="testa", recipient="testb", mode=mode,
                          root=self.root, **kw)


class TestRewriteRole(unittest.TestCase):
    def test_title_line_carries_the_new_role(self):
        out = rewrite_role("# Wren - old role\n\n## Identity\n\nx\n",
                           name="Wren", new_role="new role")
        self.assertTrue(out.startswith("# Wren - new role\n"), out)
        self.assertNotIn("old role", out)
        self.assertIn("## Identity", out)

    def test_role_section_body_is_replaced_and_neighbours_kept(self):
        md = ("# Wren - a\n\n## Identity\nI am.\n\n## Role\nold text\n"
              "more old\n\n## Voice\nDirect.\n")
        out = rewrite_role(md, name="Wren", new_role="fresh text")
        self.assertNotIn("old text", out)
        self.assertNotIn("more old", out)
        self.assertIn("## Role\n\nfresh text\n\n## Voice\nDirect.\n", out)
        self.assertIn("I am.", out)

    def test_role_section_at_end_of_file_terminates_cleanly(self):
        md = "# Wren - a\n## Identity\nI am.\n\n## Role\nold role\n"
        out = rewrite_role(md, name="Wren", new_role="new role")
        self.assertNotIn("old role", out)
        self.assertTrue(out.endswith("## Role\n\nnew role\n"), out)

    def test_no_title_and_no_section_leaves_body_unchanged(self):
        md = "just prose\n"
        self.assertEqual(rewrite_role(md, name="W", new_role="r"), md)


class TestBraidMemory(unittest.TestCase):
    def test_recipient_first_then_dated_heading_then_donor(self):
        out = braid_memory(donor_md="# D\n- entry X\n",
                           recipient_md="# R\n- entry A\n",
                           donor_name="Donor", day="2026-09-17")
        self.assertLess(out.index("entry A"), out.index("inherited from"))
        self.assertIn("## Memories inherited from Donor (2026-09-17)", out)
        self.assertLess(out.index("inherited from"), out.index("entry X"))

    def test_whitespace_is_normalised_at_the_seam(self):
        out = braid_memory(donor_md="\n\n\n# d\n", recipient_md="# r\n\n\n",
                           donor_name="X", day="2026-01-01")
        self.assertNotIn("\n\n\n\n", out)
        self.assertTrue(out.endswith("\n"))


class TestReincarnate(LifecycleCase):
    def test_refuses_an_unknown_slug_before_touching_anything(self):
        out = self._reincarnate("nobody")
        self.assertFalse(out["ok"])
        self.assertIn("unknown cousin", out["error"])
        self.assertEqual(self.flips, [])
        self.assertFalse((self.root / "data" / "lifecycle").exists())

    def test_snapshot_bequest_rewrite_flip_in_that_order(self):
        def cousin_writes():
            time.sleep(0.15)
            (self.a / "data" / "handoff.md").write_text("# bequest\n")
        threading.Thread(target=cousin_writes, daemon=True).start()
        out = self._reincarnate(timeout=3)
        self.assertTrue(out["ok"], out)
        steps = [s["step"] for s in out["steps"]]
        self.assertEqual(steps, ["snapshot", "bequest", "rewrite", "flip"])
        # Snapshot under <root>/data/lifecycle/<slug>/<timestamp>/ holding
        # the five continuity files.
        snap = pathlib.Path(out["snapshot"])
        self.assertEqual(snap.parent, self.root / "data" / "lifecycle"
                         / "testa")
        for name in ("MEMORY.md", "STATUS.md", "CLAUDE.md", "cousin.toml"):
            self.assertTrue((snap / name).is_file(), name)
        self.assertTrue((snap / "memory" / "testa-fact.md").is_file())
        self.assertIn("keeper of the ledger", (snap / "CLAUDE.md").read_text())
        # Bequest prompt reached the chat server's /api/send and the
        # cousin's handoff write was observed.
        self.assertEqual(len(_Capture.received), 1)
        self.assertEqual(_Capture.received[0]["path"], "/api/send")
        self.assertIn("handoff.md", _Capture.received[0]["payload"]["message"])
        bequest = next(s for s in out["steps"] if s["step"] == "bequest")
        self.assertTrue(bequest["sent"])
        self.assertTrue(bequest["wrote"])
        # Role rewritten in both identity files.
        claude = (self.a / "CLAUDE.md").read_text()
        self.assertTrue(claude.startswith("# Testa - mender of the fence\n"))
        cfg = tomllib.loads((self.a / "cousin.toml").read_text())
        self.assertEqual(cfg["cousin"]["role"], "mender of the fence")
        self.assertEqual(cfg["cousin"]["slug"], "testa")
        self.assertEqual(cfg["chat"]["port"], self.port)
        # Flipped through the injected seam, memory untouched.
        self.assertEqual(self.flips, ["testa"])
        self.assertEqual((self.a / "MEMORY.md").read_text(),
                         "# Testa - memory index\n- Testa remembers the first day\n")

    def test_silent_cousin_is_recorded_and_the_flip_still_runs(self):
        out = self._reincarnate(timeout=0.3)
        self.assertTrue(out["ok"], out)
        bequest = next(s for s in out["steps"] if s["step"] == "bequest")
        self.assertTrue(bequest["sent"])
        self.assertFalse(bequest["wrote"])
        self.assertEqual(self.flips, ["testa"])

    def test_unreachable_chat_server_is_a_recorded_step_not_a_crash(self):
        (self.a / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\nname = "Testa"\nrole = "r"\n\n'
            '[chat]\nport = 1\ntmux_session = "testa"\n')
        out = self._reincarnate(timeout=0.3)
        self.assertTrue(out["ok"], out)
        bequest = next(s for s in out["steps"] if s["step"] == "bequest")
        self.assertFalse(bequest["sent"])
        self.assertIn("reason", bequest)
        self.assertEqual(self.flips, ["testa"])

    def test_every_step_lands_in_the_audit_log(self):
        self._reincarnate(timeout=0.2)
        rows = self._audit()
        self.assertEqual([r["step"] for r in rows],
                         ["snapshot", "bequest", "rewrite", "flip", "done"])
        for row in rows:
            self.assertEqual(row["op"], "reincarnate")
            self.assertEqual(row["slug"], "testa")
            self.assertIn("ts", row)

    def test_a_failed_flip_makes_the_result_not_ok(self):
        out = self._reincarnate(
            timeout=0.2,
            do_flip=lambda slug: {"slug": slug, "ok": False,
                                  "error": "preflight failed"})
        self.assertFalse(out["ok"])
        self.assertIn("preflight", out["error"])
        rows = self._audit()
        self.assertFalse(next(r for r in rows if r["step"] == "flip")["ok"])

    def test_the_real_flip_runs_through_the_fake_tmux(self):
        from cousin_lib.flip import flip

        def real(slug):
            return flip(slug, tmux_bin=str(self.tmux), handoff_deadline=1,
                        halfway=0.4, settle=0)
        out = self._reincarnate(timeout=0.2, do_flip=real)
        self.assertTrue(out["ok"], out)
        calls = self.log.read_text()
        self.assertIn("new-session", calls)
        self.assertIn("BOOT PACKET FOR COUSIN: testa", calls)
        # The new packet was assembled from the rewritten identity.
        packet = self.a / "data" / "boot-packet-gen-0001.md"
        self.assertTrue(packet.is_file())
        cfg = tomllib.loads((self.a / "cousin.toml").read_text())
        self.assertEqual(cfg["cousin"]["role"], "mender of the fence")
        self.assertIn("session_id", cfg["runtime"])

    def test_cli_refuses_unknown_slug_with_exit_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = reincarnate_main(["nobody", "--new-role", "x",
                                   "--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn("unknown cousin", err.getvalue())

    def test_cli_runs_with_an_injected_flip(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                lifecycle, "_default_do_flip",
                lambda root: self._fake_flip):
            rc = reincarnate_main(
                ["testa", "--new-role", "x", "--root", str(self.root),
                 "--timeout", "0.2"])
        self.assertEqual(rc, 0)
        self.assertIn('"ok": true', out.getvalue())
        self.assertEqual(self.flips, ["testa"])


class TestTransplantRefusals(LifecycleCase):
    def test_unknown_donor_or_recipient(self):
        out = transplant(donor="ghost", recipient="testb",
                         mode="merge", root=self.root, do_flip=self._fake_flip)
        self.assertFalse(out["ok"])
        self.assertIn("unknown cousin", out["error"])
        out = transplant(donor="testa", recipient="ghost",
                         mode="merge", root=self.root, do_flip=self._fake_flip)
        self.assertFalse(out["ok"])
        self.assertIn("unknown cousin", out["error"])
        self.assertEqual(self.flips, [])

    def test_unknown_mode(self):
        out = self._transplant("possession")
        self.assertFalse(out["ok"])
        self.assertIn("unknown mode", out["error"])
        self.assertEqual(self.flips, [])
        self.assertFalse((self.root / "data" / "lifecycle").exists())

    def test_donor_equals_recipient(self):
        out = transplant(donor="testa", recipient="testa", mode="merge",
                         root=self.root, do_flip=self._fake_flip)
        self.assertFalse(out["ok"])
        self.assertIn("differ", out["error"])

    def test_cli_unknown_mode_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            transplant_main(["--donor", "testa", "--recipient", "testb",
                             "--mode", "possession", "--root", str(self.root)])
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_unknown_slug_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = transplant_main(["--donor", "ghost", "--recipient", "testb",
                                  "--mode", "merge", "--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn("unknown cousin", err.getvalue())


class TestSoulDonation(LifecycleCase):
    def test_donor_memory_replaces_recipient_memory_after_a_snapshot(self):
        out = self._transplant("soul-donation")
        self.assertTrue(out["ok"], out)
        # Recipient's memory is now the donor's.
        self.assertEqual((self.b / "MEMORY.md").read_text(),
                         (self.a / "MEMORY.md").read_text())
        self.assertTrue((self.b / "memory" / "testa-fact.md").is_file())
        self.assertFalse((self.b / "memory" / "testb-fact.md").exists())
        self.assertTrue((self.b / "memory" / "raw" / "testa-only.jsonl")
                        .is_file())
        # Recipient's body is intact.
        self.assertTrue((self.b / "CLAUDE.md").read_text()
                        .startswith("# Testb - reader of the weather"))
        cfg = tomllib.loads((self.b / "cousin.toml").read_text())
        self.assertEqual(cfg["cousin"]["name"], "Testb")
        # The recipient's old memory survives in its snapshot.
        snap = pathlib.Path(out["snapshots"]["testb"])
        self.assertIn("Testb remembers", (snap / "MEMORY.md").read_text())
        self.assertTrue((snap / "memory" / "testb-fact.md").is_file())
        self.assertIn("testa", out["snapshots"])
        # Donor untouched; both flipped, recipient last.
        self.assertTrue((self.a / "memory" / "testa-fact.md").is_file())
        self.assertEqual(self.flips, ["testa", "testb"])


class TestBodySwap(LifecycleCase):
    def test_identity_files_swap_and_memory_stays(self):
        out = self._transplant("body-swap")
        self.assertTrue(out["ok"], out)
        self.assertTrue((self.a / "CLAUDE.md").read_text()
                        .startswith("# Testb - reader of the weather"))
        self.assertTrue((self.b / "CLAUDE.md").read_text()
                        .startswith("# Testa - keeper of the ledger"))
        self.assertIn("portrait of Testb",
                      (self.a / "self-portrait.md").read_text())
        self.assertIn("portrait of Testa",
                      (self.b / "self-portrait.md").read_text())
        a_cfg = tomllib.loads((self.a / "cousin.toml").read_text())
        b_cfg = tomllib.loads((self.b / "cousin.toml").read_text())
        self.assertEqual((a_cfg["cousin"]["name"], a_cfg["cousin"]["role"]),
                         ("Testb", "reader of the weather"))
        self.assertEqual((b_cfg["cousin"]["name"], b_cfg["cousin"]["role"]),
                         ("Testa", "keeper of the ledger"))
        # Slug, port and session stay with the slot.
        self.assertEqual(a_cfg["cousin"]["slug"], "testa")
        self.assertEqual(b_cfg["cousin"]["slug"], "testb")
        self.assertEqual(a_cfg["chat"]["tmux_session"], "testa")
        # Memory did not move.
        self.assertIn("Testa remembers", (self.a / "MEMORY.md").read_text())
        self.assertIn("Testb remembers", (self.b / "MEMORY.md").read_text())
        self.assertTrue((self.a / "memory" / "testa-fact.md").is_file())
        self.assertTrue((self.b / "memory" / "testb-fact.md").is_file())
        self.assertEqual(sorted(self.flips), ["testa", "testb"])

    def test_each_slot_keeps_its_own_port_and_reply_route(self):
        # The template renders the slot's port and reply route into
        # CLAUDE.md; a swap that moves the file whole hands each cousin
        # the other's address. Canary: after a swap testa's CLAUDE.md
        # named testb's port and /api/testb_reply.
        from cousin_lib.template import render_template
        template = (pathlib.Path(lifecycle.__file__).resolve().parents[1]
                    / "templates" / "cousin-CLAUDE.template.md").read_text()
        ports = {"testa": 8211, "testb": 8222}
        for home, slug, name, role in (
                (self.a, "testa", "Testa", "keeper of the ledger"),
                (self.b, "testb", "Testb", "reader of the weather")):
            toml = (home / "cousin.toml").read_text()
            (home / "cousin.toml").write_text(
                toml.replace("port = %d" % self.port,
                             "port = %d" % ports[slug]))
            text = render_template(template, {
                "NAME": name, "SLUG": slug, "PORT": ports[slug],
                "ROLE_ONE_LINE": role, "ROLE_PARAGRAPH": role,
                "VOICE_GUIDE": "Plain."})
            # A cousin-specific section that names its own route again.
            text += "\n## Local notes\n\nPOST to `/api/%s_reply`.\n" % slug
            (home / "CLAUDE.md").write_text(text)
        out = self._transplant("body-swap")
        self.assertTrue(out["ok"], out)
        a_md = (self.a / "CLAUDE.md").read_text()
        b_md = (self.b / "CLAUDE.md").read_text()
        # The identity moved ...
        self.assertTrue(a_md.startswith("# Testb - reader of the weather"))
        self.assertIn("--from Testb", a_md)
        self.assertTrue(b_md.startswith("# Testa - keeper of the ledger"))
        # ... the address did not.
        self.assertIn("runs on port 8211 and binds `/api/testa_reply`", a_md)
        self.assertIn("runs on port 8222 and binds `/api/testb_reply`", b_md)
        self.assertNotIn("8222", a_md)
        self.assertNotIn("testb_reply", a_md)
        self.assertNotIn("8211", b_md)
        self.assertNotIn("testa_reply", b_md)
        self.assertIn("POST to `/api/testa_reply`", a_md)

    def test_missing_self_portrait_on_one_side_moves_not_crashes(self):
        (self.b / "self-portrait.md").unlink()
        out = self._transplant("body-swap")
        self.assertTrue(out["ok"], out)
        self.assertFalse((self.a / "self-portrait.md").exists())
        self.assertIn("portrait of Testa",
                      (self.b / "self-portrait.md").read_text())


class TestMerge(LifecycleCase):
    def test_memory_braided_and_raw_unioned(self):
        out = self._transplant("merge")
        self.assertTrue(out["ok"], out)
        merged = (self.b / "MEMORY.md").read_text()
        self.assertIn("Testb remembers", merged)
        self.assertIn("## Memories inherited from Testa (", merged)
        self.assertLess(merged.index("Testb remembers"),
                        merged.index("Testa remembers"))
        raw = self.b / "memory" / "raw"
        # Besides the union, today's file holds the framework's own L1
        # note of the transplant (tests/test_memory_level_writers.py).
        today = time.strftime("%Y-%m-%d") + ".jsonl"
        notes = [json.loads(l) for l in
                 (raw / today).read_text().splitlines()]
        self.assertEqual({n["topic"] for n in notes},
                         {"framework:transplant"})
        names = sorted(p.name for p in raw.iterdir() if p.name != today)
        self.assertEqual(names, ["2026-09-01.jsonl", "testa-only.jsonl",
                                 "testb-only.jsonl"])
        shared = (raw / "2026-09-01.jsonl").read_text().splitlines()
        self.assertEqual(len(shared), 2)
        self.assertIn("testa day one", "\n".join(shared))
        self.assertIn("testb day one", "\n".join(shared))
        # Identity and the recipient's own memory files stay.
        self.assertTrue((self.b / "CLAUDE.md").read_text()
                        .startswith("# Testb - reader"))
        self.assertTrue((self.b / "memory" / "testb-fact.md").is_file())
        # Donor untouched.
        self.assertEqual((self.a / "MEMORY.md").read_text(),
                         "# Testa - memory index\n- Testa remembers the first day\n")
        self.assertEqual(sorted(self.flips), ["testa", "testb"])

    def test_union_does_not_duplicate_identical_lines(self):
        line = json.dumps({"who": "both", "text": "same"}) + "\n"
        (self.a / "memory" / "raw" / "2026-09-01.jsonl").write_text(line)
        (self.b / "memory" / "raw" / "2026-09-01.jsonl").write_text(line)
        self._transplant("merge")
        self.assertEqual(
            (self.b / "memory" / "raw" / "2026-09-01.jsonl").read_text(),
            line)

    def test_audit_names_both_parties_and_the_mode(self):
        self._transplant("merge")
        rows = self._audit()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["op"], "transplant")
            self.assertEqual(row["donor"], "testa")
            self.assertEqual(row["recipient"], "testb")
            self.assertEqual(row["mode"], "merge")
        self.assertEqual([r["step"] for r in rows][-1], "done")
        self.assertIn("apply", [r["step"] for r in rows])

    def test_cli_merge_with_injected_flip(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                lifecycle, "_default_do_flip",
                lambda root: self._fake_flip):
            rc = transplant_main(
                ["--donor", "testa", "--recipient", "testb", "--mode",
                 "merge", "--root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn('"ok": true', out.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestReincarnateOnTheRunnerLane(HermeticCase):
    def test_rolls_over_into_the_new_role_carrying_the_bequest(self):
        from cousin_lib import lifecycle, memory
        from cousin_lib.runner.main import hold_lock
        from cousin_lib.runner.sdk import SdkRunner
        from tests.runner._home import temp_home
        from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        (home / "CLAUDE.md").write_text("# Wren - keeps the ledgers\n\n## Identity\n\nWren keeps the ledgers.\n")
        memory.remember(home, "ledger", "March is open")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}); p.start(); self.addCleanup(p.stop)
        clients = []

        def factory(options):
            clients.append(ScriptedClient(options, [[init_msg(session="s-%d" % (len(clients) + 1)),
                                                     assistant(text="ok"), result()]] * 4))
            return clients[-1]
        sent = []
        with hold_lock(home):
            r = SdkRunner(home, client_factory=factory, handoff_deadline_s=0.5)
            r.start(); self.addCleanup(lambda: r.stop(timeout=5))
            out = lifecycle.reincarnate("wren", new_role="audits the audits", root=root,
                                        send=lambda cfg, text: sent.append(text))
        self.assertTrue(out["ok"], out)
        self.assertEqual(sent, [])                                        # no chat-server prompt
        bequest = [s for s in out["steps"] if s.get("step") == "bequest"][0]
        self.assertTrue(bequest["carried"])
        asked = [q["message"]["content"][0]["text"] for q in clients[0].queries]
        self.assertTrue(any("You are about to be reincarnated" in t for t in asked))  # carried
        self.assertIn("# Wren - audits the audits", clients[-1].options.system_prompt["append"])
        raw = "".join(p.read_text() for p in (home / "memory" / "raw").glob("*.jsonl"))
        self.assertIn("March is open", raw)                               # memory kept

    def test_the_runner_bequest_is_a_bequest(self):
        from cousin_lib.runner import rollover
        self.assertTrue(rollover.is_bequest(lifecycle.BEQUEST_PROMPT_RUNNER.format(timeout=300)))
