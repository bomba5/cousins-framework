"""Configuration seam: identity, ports, and operator profile.

Everything operator-specific or instance-specific is configuration; the
framework is code. A cousin's identity lives in <home>/cousin.toml. The
operator is optional by design - the null profile is "no operator", never
a defaulted human being.
"""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class MissingConfigError(Exception):
    """Required configuration is absent. Fail loud: a silent fallback here
    becomes somebody's home directory or somebody's name in a log."""


@dataclass
class CousinConfig:
    home: Path
    slug: str
    name: str
    chat_port: int | None
    operator_name: str | None
    chat_host: str | None = None
    peer_visible: bool = True
    tmux_session: str = ""
    memory_scope: str = "private"
    heartbeat_seconds: int = 3600
    type: str = "cousin"
    flip_at: str | None = None

    @classmethod
    def load(cls, home):
        home = Path(home)
        toml_path = home / "cousin.toml"
        if not toml_path.is_file():
            raise MissingConfigError("no cousin.toml under %s" % home)
        data = tomllib.loads(toml_path.read_text())
        cousin = data.get("cousin", {})
        slug = cousin.get("slug")
        if not slug:
            raise MissingConfigError("cousin.slug missing in %s" % toml_path)
        return cls(
            home=home,
            slug=slug,
            name=cousin.get("name", slug.capitalize()),
            chat_port=data.get("chat", {}).get("port"),
            operator_name=data.get("operator", {}).get("name"),
            chat_host=data.get("chat", {}).get("host"),
            peer_visible=bool(cousin.get("peer_visible", True)),
            tmux_session=data.get("chat", {}).get("tmux_session", slug),
            memory_scope=data.get("memory", {}).get("scope", "private"),
            heartbeat_seconds=int(
                data.get("heartbeat", {})
                .get("context_beat_seconds", 3600)),
            type=cousin.get("type", "cousin"),
            flip_at=data.get("lifecycle", {}).get("flip_at"),
        )

    @classmethod
    def from_env(cls):
        home = os.environ.get("COUSIN_HOME")
        if not home:
            raise MissingConfigError(
                "COUSIN_HOME is not set; every cousin process runs with its "
                "home in the environment"
            )
        return cls.load(home)

    def require_chat_port(self):
        if self.chat_port is None:
            raise MissingConfigError(
                "chat.port missing in %s" % (self.home / "cousin.toml")
            )
        return self.chat_port


class FrameworkConfig:
    """An install's shared layout. The filesystem is the registry: a cousin
    exists iff cousins/<slug>/cousin.toml exists under the root. No service
    has to be running for the fleet to be enumerable."""

    def __init__(self, root):
        self.root = Path(root)

    @classmethod
    def from_env(cls):
        root = os.environ.get("FRAMEWORK_ROOT")
        if not root:
            raise MissingConfigError(
                "FRAMEWORK_ROOT is not set; the framework root locates the "
                "cousin registry and shared configuration"
            )
        return cls(root)

    def list_cousins(self):
        base = self.root / "cousins"
        rows = []
        if not base.is_dir():
            return rows
        for entry in sorted(base.iterdir()):
            if (entry / "cousin.toml").is_file():
                rows.append(CousinConfig.load(entry))
        return rows
