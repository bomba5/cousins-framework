"""Network guard: the address allowlist every request passes first.

Identity above the network layer is client-asserted, so the address
allowlist is the chat server's only boundary. Defaults are loopback plus
the RFC1918 private ranges; an install EXTENDS the list, and no
configuration can remove loopback - local CLIs must always work.
"""
import ipaddress
import json

_DEFAULT_NETWORKS = [
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8", "::1/128",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    )
]


class NetGuard:
    """Callable: address string in, allow/deny out. Unparsable client
    addresses are denied (fail closed); unparsable configured CIDRs are
    skipped (one typo must not lock out the operator's whole config)."""

    def __init__(self, extra_cidrs=()):
        self.networks = list(_DEFAULT_NETWORKS)
        for cidr in extra_cidrs:
            try:
                self.networks.append(ipaddress.ip_network(cidr))
            except ValueError:
                continue

    @classmethod
    def from_config(cls, framework_root):
        """Build from <framework root>/config/net-allowlist.json
        ({"allow": [cidr, ...]}). A missing or unreadable file means
        defaults only."""
        path = framework_root / "config" / "net-allowlist.json"
        try:
            data = json.loads(path.read_text())
            extra = data.get("allow", [])
        except (OSError, ValueError):
            extra = []
        return cls(extra_cidrs=extra)

    def __call__(self, address):
        try:
            addr = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(addr in network for network in self.networks)
