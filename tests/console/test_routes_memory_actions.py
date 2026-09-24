"""The memory explorer's operator actions (WP-E): search with its legs,
the write forms (remember, decide) with a cite the console fills,
history, the review gate's keep/drop queue, the maintenance runs as a
long operation, the self-portrait gate, and the read-only callbacks and
capsules lists."""
import hashlib
import json
import time
import unittest

from cousin_lib import memory, review_gate, self_portrait
from cousin_lib.console import auth, longop
from tests.console._harness import ConsoleCase


def _raw(home, name, rows):
    path = home / "memory" / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


class ActionsCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.home = self.cousin("wren", operator="Ana")
        _raw(self.home, "2026-09-01.jsonl", [
            {"topic": "kestrel", "content": "the kestrel nests on the roof",
             "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2026-09-01T10:00:00+00:00"},
            {"topic": "kestrel", "content": "the kestrel nests in the barn",
             "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2026-09-02T10:00:00+00:00"},
        ])
        (self.home / "memory" / "birds.md").write_text(
            "# Birds\n\nA kestrel hovers before it dives.\n")

    def users(self, *names):
        users = auth.Users(self.root / "config" / "console-users.json")
        for name in names:
            users.set_password(name, "correct horse")

    def login(self, name):
        status, _ = self.post("/api/auth/login",
                              {"user": name, "password": "correct horse"})
        self.assertEqual(status, 200)

    def wait_op(self, slug="wren", timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = longop.status(self.server, slug)
            if op and op["status"] != "running":
                return op
            time.sleep(0.02)
        self.fail("the op did not finish")


class Search(ActionsCase):
    def test_keyword_hits_with_legs_and_home_relative_paths(self):
        self.serve()
        status, body = self.get("/api/memory/wren/search?q=kestrel&top=10")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["semantic"], "off")
        self.assertIsNone(body["notice"])
        hits = body["hits"]
        self.assertTrue(hits)
        text = json.dumps(body)
        self.assertNotIn(str(self.root), text, "no absolute path is served")
        files = [h for h in hits if h["collection"] == "memory"]
        self.assertEqual(files[0]["rel"], "memory/birds.md")
        self.assertEqual(files[0]["legs"], ["keyword"])
        raws = [h for h in hits if h["collection"] == "raw"]
        self.assertEqual(len(raws), 2)
        self.assertTrue(raws[0]["rel"].startswith("memory/raw/2026-09-01.jsonl#"))
        self.assertEqual(raws[0]["entry"]["topic"], "kestrel")
        self.assertIn(raws[0]["entry"]["level"], ("L3_COUSIN_CONCLUSION",))
        # a console search is not the cousin's: it reinforces nothing
        self.assertFalse((self.home / "memory" / ".recall-log.jsonl").exists())

    def test_refusals(self):
        self.serve()
        self.assertEqual(self.get("/api/memory/wren/search")[0], 400)
        self.assertEqual(self.get("/api/memory/wren/search?q=x&collection=etc")[0], 400)
        self.assertEqual(self.get("/api/memory/nobody/search?q=x")[0], 404)

    def test_a_broken_embedding_config_is_said(self):
        (self.root / "config" / "embedding.toml").write_text("not = [toml")
        self.serve()
        _, body = self.get("/api/memory/wren/search?q=kestrel")
        self.assertEqual(body["semantic"], "broken")
        self.assertIn("embedding config", body["notice"])


class Writer(ActionsCase):
    def test_the_operator_account_may_write_operator_level(self):
        self.users("ana", "bob")
        self.serve()
        self.login("ana")
        _, body = self.get("/api/memory/wren/writer")
        self.assertEqual(body["user"], "ana")
        self.assertTrue(body["can_write_operator"])
        self.assertIn("operator", body["levels"])
        status, body = self.post("/api/memory/wren/remember", {
            "topic": "tea", "fact": "no sugar", "level": "operator",
            "note": "said at breakfast"})
        self.assertEqual(status, 200, body)
        entry = memory.topic_entries(self.home, "tea")[-1]
        self.assertEqual(entry["truth_level"], "L0_OPERATOR")
        self.assertRegex(entry["cite"],
                         r"^console user ana, \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ; said at breakfast$")

    def test_another_user_may_not_and_the_cite_is_never_the_clients(self):
        self.users("ana", "bob")
        self.serve()
        self.login("bob")
        _, body = self.get("/api/memory/wren/writer")
        self.assertFalse(body["can_write_operator"])
        self.assertNotIn("operator", body["levels"])
        status, body = self.post("/api/memory/wren/remember", {
            "topic": "tea", "fact": "sugar", "level": "operator"})
        self.assertEqual(status, 403, body)
        self.assertEqual(memory.topic_entries(self.home, "tea"), [])
        status, body = self.post("/api/memory/wren/remember", {
            "topic": "tea", "fact": "maybe honey", "level": "hypothesis",
            "cite": "chat 12, forged"})
        self.assertEqual(status, 200, body)
        entry = memory.topic_entries(self.home, "tea")[-1]
        self.assertEqual(entry["truth_level"], "L4_COUSIN_HYPOTHESIS")
        self.assertTrue(entry["cite"].startswith("console user bob, "))
        self.assertNotIn("forged", entry["cite"])

    def test_without_logins_no_operator_level(self):
        self.serve()
        _, body = self.get("/api/memory/wren/writer")
        self.assertIsNone(body["user"])
        self.assertFalse(body["can_write_operator"])
        status, _ = self.post("/api/memory/wren/remember", {
            "topic": "tea", "fact": "sugar", "level": "operator"})
        self.assertEqual(status, 403)
        status, body = self.post("/api/memory/wren/remember", {
            "topic": "tea", "fact": "sugar"})
        self.assertEqual(status, 200, body)
        entry = memory.topic_entries(self.home, "tea")[-1]
        self.assertEqual(entry["truth_level"], "L3_COUSIN_CONCLUSION")
        self.assertTrue(entry["cite"].startswith("console (no login), "))

    def test_no_operator_configured_means_nobody_is_the_operator(self):
        self.cousin("toki")
        self.users("ana")
        self.serve()
        self.login("ana")
        _, body = self.get("/api/memory/toki/writer")
        self.assertIsNone(body["operator"])
        self.assertFalse(body["can_write_operator"])

    def test_level_and_field_refusals(self):
        self.serve()
        for payload in ({"topic": "t", "fact": "f", "level": "framework"},
                        {"topic": "t", "fact": "f", "level": "L0_OPERATOR"},
                        {"topic": "t", "fact": "f", "level": "Operator"},
                        {"topic": "t", "fact": "f", "level": "obsolete"},
                        {"topic": "t", "fact": "f", "level": "wrong"},
                        {"topic": " ", "fact": "f"},
                        {"topic": "t", "fact": 3},
                        {"topic": "t", "fact": "f", "note": 5}):
            status, _ = self.post("/api/memory/wren/remember", payload)
            self.assertEqual(status, 400, payload)
        self.assertEqual(memory.topic_entries(self.home, "t"), [])

    def test_decide_logs_the_decision_and_its_raw_copy_with_the_cite(self):
        self.users("ana")
        self.serve()
        self.login("ana")
        status, body = self.post("/api/memory/wren/decide", {
            "topic": "roof", "decision": "fix it in spring",
            "reasoning": "the frost", "level": "operator"})
        self.assertEqual(status, 200, body)
        log = (self.home / "data" / "decisions.jsonl").read_text()
        self.assertIn("fix it in spring", log)
        entry = memory.topic_entries(self.home, "roof")[-1]
        self.assertEqual(entry["truth_level"], "L0_OPERATOR")
        self.assertTrue(entry["cite"].startswith("console user ana, "))
        status, _ = self.post("/api/memory/wren/decide", {
            "topic": "roof", "decision": "x"})
        self.assertEqual(status, 400)


class History(ActionsCase):
    def test_a_topics_claims_with_their_valid_time(self):
        self.serve()
        status, body = self.get("/api/memory/wren/history?topic=kestrel")
        self.assertEqual(status, 200, body)
        claims = body["claims"]
        self.assertEqual([c["content"] for c in claims],
                         ["the kestrel nests on the roof",
                          "the kestrel nests in the barn"])
        self.assertTrue(all(c["valid_to"] is None for c in claims))
        first = claims[0]["id"]
        self.post("/api/memory/wren/obsolete",
                  {"topic": "kestrel", "why": "it moved", "entry": first})
        _, body = self.get("/api/memory/wren/history?topic=kestrel")
        self.assertIsNotNone(body["claims"][0]["valid_to"])
        self.assertIsNone(body["claims"][1]["valid_to"])
        self.assertEqual(self.get("/api/memory/wren/history")[0], 400)


class Review(ActionsCase):
    def hold(self):
        review_gate.begin(self.home, now=time.time() - 30)
        for i in range(4):
            memory.remember(self.home, "batch-%d" % i, "fact %d" % i)
        memory.remember(self.home, "said", "operator said", level="operator",
                        cite="chat 1")
        return review_gate.hold_new(self.home, limit=3)

    def test_the_queue_and_a_verdict_as_the_logged_in_user(self):
        held = self.hold()
        self.assertEqual(len(held), 5)
        self.users("ana", "bob")
        self.serve()
        self.login("ana")
        status, body = self.get("/api/memory/wren/review")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["held"]), 5)
        self.assertEqual(body["batch"], 3)
        ids = [r["id"] for r in body["held"]]
        # more than a couple of verdicts: the cousin's long operation
        status, body = self.post("/api/memory/wren/review", {
            "verdicts": {ids[0]: "keep", ids[1]: "drop", "nope": "keep"},
            "why": "tidy"})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "memory-review")
        op = self.wait_op()
        self.assertEqual(op["status"], "done", op)
        body = op["result"]
        self.assertEqual(body["done"], {ids[0]: "keep", ids[1]: "drop"})
        self.assertIn("nope", body["errors"])
        self.assertTrue(body["effects"]["distilled"])
        left = {r["id"] for r in review_gate.pending(self.home)}
        self.assertEqual(left, set(ids[2:]))
        records = [e for e in memory._all_raw(self.home)
                   if e.get("released") in (ids[0], ids[1])]
        self.assertTrue(all(e["by"] == "console:ana" for e in records))

    def test_only_the_operator_drops_an_operator_level_entry(self):
        self.hold()
        said = next(r for r in review_gate.pending(self.home)
                    if r["topic"] == "said")
        self.users("ana", "bob")
        self.serve()
        self.login("bob")
        status, body = self.post("/api/memory/wren/review",
                                 {"verdicts": {said["id"]: "drop"}})
        self.assertEqual(status, 200)
        self.assertIn(said["id"], body["errors"])
        self.assertIn(said["id"], {r["id"] for r in review_gate.pending(self.home)})

    def test_one_or_two_verdicts_answer_at_once(self):
        held = self.hold()
        self.users("ana")
        self.serve()
        self.login("ana")
        status, body = self.post("/api/memory/wren/review",
                                 {"verdicts": {held[0]["id"]: "keep",
                                               held[1]["id"]: "keep"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["done"]), 2)

    def test_a_verdict_needs_a_login(self):
        held = self.hold()
        self.serve()
        status, body = self.post("/api/memory/wren/review",
                                 {"verdicts": {held[0]["id"]: "keep"}})
        self.assertEqual(status, 403, body)
        self.assertEqual(len(review_gate.pending(self.home)), 5)

    def test_malformed_verdicts(self):
        self.users("ana")
        self.serve()
        self.login("ana")
        for payload in ({}, {"verdicts": []}, {"verdicts": {"a": "maybe"}},
                        {"verdicts": {"a": "keep"}, "why": 3},
                        {"verdicts": {"a": "keep"}, "why": "x" * 501}):
            self.assertEqual(self.post("/api/memory/wren/review", payload)[0],
                             400, payload)


class Maintenance(ActionsCase):
    def test_distill_runs_as_a_long_op(self):
        self.serve()
        status, body = self.post("/api/memory/wren/maintain", {"action": "distill"})
        self.assertEqual(status, 202, body)
        self.assertEqual(body["op"]["kind"], "memory-distill")
        op = self.wait_op()
        self.assertEqual(op["status"], "done", op)
        self.assertGreaterEqual(op["result"]["report"]["entries"], 2)
        self.assertTrue((self.home / "memory" / "distilled").is_dir())

    def test_reindex_builds_the_keyword_index(self):
        self.serve()
        self.post("/api/memory/wren/maintain", {"action": "reindex"})
        op = self.wait_op()
        self.assertEqual(op["status"], "done", op)
        self.assertGreaterEqual(op["result"]["indexed"], 3)
        self.assertEqual(op["result"]["semantic"], "off")

    def test_compact_raw_and_the_index_preview(self):
        _raw(self.home, "2020-01-05.jsonl", [
            {"topic": "old", "content": "long ago", "truth_level": "L2_TOOL",
             "timestamp": "2020-01-05T10:00:00+00:00"}])
        self.serve()
        self.post("/api/memory/wren/maintain", {"action": "compact-raw"})
        op = self.wait_op()
        self.assertEqual(op["status"], "done", op)
        self.assertEqual(op["result"]["report"]["folded_days"], 1)
        (self.home / "MEMORY.md").write_text("- [x](x.md) - a pointer\n")
        self.post("/api/memory/wren/maintain",
                  {"action": "compact-index", "dry_run": True})
        op = self.wait_op()
        self.assertEqual(op["status"], "done", op)
        self.assertTrue(op["params"]["dry_run"])
        self.assertEqual(op["result"]["report"]["retired"], [])

    def test_refusals_and_busy(self):
        self.serve()
        self.assertEqual(self.post("/api/memory/wren/maintain",
                                   {"action": "rm -rf"})[0], 400)
        self.assertEqual(self.post("/api/memory/nobody/maintain",
                                   {"action": "distill"})[0], 404)
        hold = longop.exclusive(self.server, "wren", "flip")
        try:
            self.assertEqual(self.post("/api/memory/wren/maintain",
                                       {"action": "distill"})[0], 409)
        finally:
            hold.release()


class Portrait(ActionsCase):
    def test_synthesize_edit_diff_and_commit_with_a_typed_confirmation(self):
        self.users("ana")
        self.serve()
        self.login("ana")
        status, body = self.get("/api/memory/wren/portrait")
        self.assertEqual(status, 200)
        self.assertFalse(body["candidate_exists"])
        status, body = self.post("/api/memory/wren/portrait/synthesize")
        self.assertEqual(status, 200, body)
        self.assertIn("# Cousin Self-Portrait: wren", body["candidate"])
        # a second synthesize would overwrite the edits: it asks first
        self.assertEqual(self.post("/api/memory/wren/portrait/synthesize")[0], 409)
        status, body = self.post("/api/memory/wren/portrait/candidate",
                                 {"text": "# Wren\n\nkind and brief\n"})
        self.assertEqual(status, 200, body)
        sha = body["candidate_sha"]
        self.assertEqual(sha, hashlib.sha256(b"# Wren\n\nkind and brief\n").hexdigest()[:16])
        _, body = self.get("/api/memory/wren/portrait")
        self.assertIn("+kind and brief", body["diff"])
        for payload in ({"confirm": "wrenn", "sha": sha},
                        {"confirm": "wren", "sha": "0" * 16}):
            status, _ = self.post("/api/memory/wren/portrait/commit", payload)
            self.assertEqual(status, 409 if payload["sha"] != sha else 400, payload)
        self.assertFalse(self_portrait.committed_path(self.home).exists())
        status, body = self.post("/api/memory/wren/portrait/commit",
                                 {"confirm": "wren", "sha": sha})
        self.assertEqual(status, 200, body)
        self.assertEqual(self_portrait.committed_path(self.home).read_text(),
                         "# Wren\n\nkind and brief\n")
        self.assertFalse(self_portrait.candidate_path(self.home).exists())

    def test_commit_needs_a_login(self):
        self.serve()
        self.post("/api/memory/wren/portrait/candidate", {"text": "# W\n"})
        sha = hashlib.sha256(b"# W\n").hexdigest()[:16]
        status, _ = self.post("/api/memory/wren/portrait/commit",
                              {"confirm": "wren", "sha": sha})
        self.assertEqual(status, 403)
        self.assertFalse(self_portrait.committed_path(self.home).exists())

    def test_a_candidate_linked_out_of_the_home_is_never_served(self):
        outside = self.root / "outside.txt"
        outside.write_text("not the cousin's")
        self_portrait.candidate_path(self.home).symlink_to(outside)
        self.serve()
        status, body = self.get("/api/memory/wren/portrait")
        self.assertEqual(status, 403)
        self.assertNotIn("not the cousin's", json.dumps(body))

    def test_synthesize_never_writes_through_a_link(self):
        outside = self.root / "victim.txt"          # dangling: not there yet
        self_portrait.candidate_path(self.home).symlink_to(outside)
        self.serve()
        for payload in ({}, {"replace": True}):
            status, _ = self.post("/api/memory/wren/portrait/synthesize", payload)
            self.assertEqual(status, 403, payload)
            self.assertFalse(outside.exists(), payload)
        outside.write_text("mine")                  # a live link, inside or not
        for path, payload in (("synthesize", {"replace": True}),
                              ("candidate", {"text": "# W\n"})):
            status, _ = self.post("/api/memory/wren/portrait/" + path, payload)
            self.assertEqual(status, 403, path)
        self.assertEqual(outside.read_text(), "mine")

    def test_a_link_inside_the_home_is_refused_too(self):
        inside = self.home / "notes.md"
        inside.write_text("a note")
        self_portrait.candidate_path(self.home).symlink_to(inside)
        self.serve()
        status, _ = self.post("/api/memory/wren/portrait/synthesize",
                              {"replace": True})
        self.assertEqual(status, 403)
        self.assertEqual(inside.read_text(), "a note")

    def test_candidate_refusals(self):
        self.serve()
        for payload in ({}, {"text": 3}, {"text": "x" * 70000}):
            self.assertEqual(self.post("/api/memory/wren/portrait/candidate",
                                       payload)[0], 400, payload)
        self.assertEqual(self.post("/api/memory/wren/portrait/commit",
                                   {"confirm": "wren", "sha": "x"})[0], 403)


class RetiringTheOperatorsWord(ActionsCase):
    """An operator-level claim is the operator's to retire: an entry-level
    mark on it, or a topic-level mark on a topic with a live one, needs the
    operator account."""

    def setUp(self):
        super().setUp()
        _raw(self.home, "2026-09-03.jsonl", [
            {"topic": "tea", "content": "no sugar", "truth_level": "L0_OPERATOR",
             "cite": "chat 1", "timestamp": "2026-09-03T10:00:00+00:00"},
            {"topic": "tea", "content": "two sugars", "truth_level": "L3_COUSIN_CONCLUSION",
             "timestamp": "2026-09-03T11:00:00+00:00"}])

    def claim(self, level):
        return next(r["id"] for r in memory.validity(self.home)
                    if r["topic"] == "tea" and r["truth_level"] == level)

    def test_another_user_may_not(self):
        self.users("ana", "bo")
        self.serve()
        self.login("bo")
        for payload in ({"topic": "tea", "why": "x", "entry": self.claim("L0_OPERATOR")},
                        {"topic": "tea", "why": "x"}):
            status, body = self.post("/api/memory/wren/obsolete", payload)
            self.assertEqual(status, 403, (payload, body))
        # the cousin's own claim on the topic is anyone's to retire
        status, _ = self.post("/api/memory/wren/obsolete", {
            "topic": "tea", "why": "x", "entry": self.claim("L3_COUSIN_CONCLUSION")})
        self.assertEqual(status, 200)
        self.assertEqual(sum(1 for e in memory.topic_entries(self.home, "tea")
                             if e.get("truth_level") == "L5_OBSOLETE"), 1)

    def test_nobody_without_a_login(self):
        self.serve()
        status, _ = self.post("/api/memory/wren/obsolete", {"topic": "tea", "why": "x"})
        self.assertEqual(status, 403)

    def test_the_operator_may(self):
        self.users("ana")
        self.serve()
        self.login("ana")
        status, body = self.post("/api/memory/wren/obsolete", {
            "topic": "tea", "why": "x", "entry": self.claim("L0_OPERATOR")})
        self.assertEqual(status, 200, body)
        status, body = self.post("/api/memory/wren/obsolete", {"topic": "tea", "why": "all"})
        self.assertEqual(status, 200, body)


class Moments(ActionsCase):
    def test_callbacks_and_capsules_read_only(self):
        from cousin_lib import callback, capsule
        callback.tag(self.home, "the roof leak joke", category="house")
        capsule.write_capsule(self.home, conclusion="fix in spring",
                              evidence=["frost"], rejected=["now"])
        self.serve()
        _, body = self.get("/api/memory/wren/callbacks")
        self.assertEqual(body["callbacks"][0]["moment"], "the roof leak joke")
        _, body = self.get("/api/memory/wren/capsules")
        self.assertEqual(body["capsules"][0]["conclusion"], "fix in spring")
        self.assertEqual(self.get("/api/memory/wren/callbacks?limit=99999")[0], 200)


class BehindTheLogin(ActionsCase):
    def test_every_new_route_is_401_without_a_session(self):
        self.users("ana")
        self.serve()
        for path in ("/api/memory/wren/search?q=x", "/api/memory/wren/writer",
                     "/api/memory/wren/history?topic=x", "/api/memory/wren/review",
                     "/api/memory/wren/portrait", "/api/memory/wren/callbacks",
                     "/api/memory/wren/capsules"):
            self.assertEqual(self.get(path)[0], 401, path)
        for path in ("remember", "decide", "review", "maintain",
                     "portrait/synthesize", "portrait/candidate",
                     "portrait/commit"):
            self.assertEqual(self.post("/api/memory/wren/" + path, {})[0], 401, path)


if __name__ == "__main__":
    unittest.main()
