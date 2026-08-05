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
