"""Per-surface outbound content filter.

The framework ships no banned vocabulary and no trusted peers: policy is
configuration, and an install without a policy file has an inert filter.
Planted terms here are fictional.
"""
import json
import pathlib
import tempfile
import unittest

from cousin_lib.outbound_filter import FilterBlocked, OutboundPolicy


def _root(policy=None):
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    if policy is not None:
        (root / "config").mkdir()
        (root / "config" / "outbound-filter.json").write_text(json.dumps(policy))
    return tmp, root


class TestOutboundPolicy(unittest.TestCase):
    def _policy(self, policy=None):
        tmp, root = _root(policy)
        self.addCleanup(tmp.cleanup)
        return OutboundPolicy.load(root)

    def test_no_policy_file_means_an_inert_filter(self):
        p = self._policy(None)
        self.assertEqual(p.scan("anything at all"), [])
        p.check("anything at all", from_slug="a", dest_slug="b")  # no raise

    def test_terms_match_on_word_boundaries_case_insensitive(self):
        p = self._policy({"terms": ["zorblatt"]})
        self.assertEqual(p.scan("Ask ZORBLATT."), ["zorblatt"])
        self.assertEqual(p.scan("zorblattify everything"), [])

    def test_protected_slugs_are_banned_terms(self):
        p = self._policy({"protected": ["quiet"]})
        self.assertEqual(p.scan("tell quiet about it"), ["quiet"])

    def test_sending_to_a_protected_cousin_is_internal(self):
        p = self._policy({"protected": ["quiet"], "terms": ["zorblatt"]})
        p.check("zorblatt", from_slug="wren", dest_slug="quiet")  # no raise

    def test_protected_outbound_to_trusted_peer_is_internal(self):
        p = self._policy({"protected": ["quiet"], "trusted_peers": ["gate"]})
        p.check("quiet things", from_slug="quiet", dest_slug="gate")  # no raise

    def test_protected_outbound_to_untrusted_peer_is_scanned(self):
        p = self._policy({"protected": ["quiet"]})
        with self.assertRaises(FilterBlocked) as ctx:
            p.check("quiet things", from_slug="quiet", dest_slug="wren")
        self.assertEqual(ctx.exception.terms, ["quiet"])

    def test_surface_additions_gate_only_their_surface(self):
        p = self._policy(
            {"surfaces": {"mail": {"add": ["quibbleton"]}}}
        )
        with self.assertRaises(FilterBlocked):
            p.check("quibbleton", from_slug="a", dest_slug="b", surface="mail")
        p.check("quibbleton", from_slug="a", dest_slug="b", surface="chat")

    def test_override_env_disables_blocking(self):
        p = self._policy({"terms": ["zorblatt"]})
        import unittest.mock as mock

        with mock.patch.dict("os.environ", {"COUSIN_FILTER_OVERRIDE": "1"}):
            p.check("zorblatt", from_slug="a", dest_slug="b")  # no raise


if __name__ == "__main__":
    unittest.main()
