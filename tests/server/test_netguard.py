"""Network guard: the address allowlist every request passes first.

The trust model is the address, nothing else - identity above it is
client-asserted, so this list is the only boundary.
"""
import ipaddress
import json
import pathlib
import tempfile
import unittest

from cousin_lib.server.netguard import NetGuard

# Example hosts are derived from the sanctioned range constants at
# runtime so this file carries no host-shaped address literal.
_PRIVATE_HOSTS = [
    str(ipaddress.ip_network(cidr)[7])
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
]


class TestDefaults(unittest.TestCase):
    def test_loopback_is_allowed(self):
        guard = NetGuard()
        self.assertTrue(guard("127.0.0.1"))
        self.assertTrue(guard("::1"))

    def test_private_ranges_are_allowed(self):
        guard = NetGuard()
        for addr in _PRIVATE_HOSTS:
            self.assertTrue(guard(addr), addr)

    def test_public_addresses_are_denied(self):
        guard = NetGuard()
        self.assertFalse(guard("8.8.8.8"))

    def test_unparsable_client_address_is_denied_fail_closed(self):
        guard = NetGuard()
        self.assertFalse(guard("not-an-address"))
        self.assertFalse(guard(""))


class TestExtensions(unittest.TestCase):
    def test_extra_cidrs_add_to_the_defaults(self):
        guard = NetGuard(extra_cidrs=["203.0.113.0/24"])
        self.assertTrue(guard("203.0.113.9"))
        self.assertFalse(guard("8.8.8.8"))

    def test_extensions_can_never_remove_loopback(self):
        # Local CLIs must always work, whatever an install configures.
        guard = NetGuard(extra_cidrs=["203.0.113.0/24"])
        self.assertTrue(guard("127.0.0.1"))

    def test_unparsable_configured_cidr_is_skipped_not_fatal(self):
        guard = NetGuard(extra_cidrs=["garbage/99", "203.0.113.0/24"])
        self.assertTrue(guard("203.0.113.9"))


class TestFromConfig(unittest.TestCase):
    def _root(self, allowlist=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        if allowlist is not None:
            (root / "config").mkdir()
            (root / "config" / "net-allowlist.json").write_text(
                json.dumps(allowlist)
            )
        return root

    def test_reads_the_install_allowlist(self):
        root = self._root({"allow": ["203.0.113.0/24"]})
        guard = NetGuard.from_config(root)
        self.assertTrue(guard("203.0.113.9"))

    def test_missing_file_means_defaults_only(self):
        guard = NetGuard.from_config(self._root())
        self.assertTrue(guard("127.0.0.1"))
        self.assertFalse(guard("203.0.113.9"))


if __name__ == "__main__":
    unittest.main()
