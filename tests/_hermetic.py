"""Hermetic environment for every test.

The suite is documented as the gate an operator runs, and an operator's
shell exports the install's FRAMEWORK_ROOT (and a cousin's shell its
COUSIN_HOME). A test that reads either follows it into the live
install: its config/embedding.toml, its registry, its databases. That
is how a finished install failed three memory-search tests that pass
on a clean checkout.

install() wraps unittest.TestCase.run so every test, in every module,
runs with the variables below removed from os.environ, and with the
whole environment restored afterwards (a CLI entry point under test
may export FRAMEWORK_ROOT for its own children; that must not leak into
the next test). A test that needs one sets it itself, pointing at its
own temporary directory. HermeticCase is the same guarantee as an
explicit base class, for a module that wants to say so.

install() runs from tests/__init__.py, which is imported during
discovery (test_hermetic imports it), before any test runs.
"""
import os
import unittest
from unittest import mock

# Every variable the framework reads to find an install, a cousin, a
# tmux server, a filter override or a supervisor.
HERMETIC_VARS = ("FRAMEWORK_ROOT", "COUSIN_HOME", "COUSIN_SLUG",
                 "COUSIN_TMUX_SOCKET", "COUSIN_FILTER_OVERRIDE",
                 "INVOCATION_ID")

_MARK = "_cousin_hermetic"


def hermetic_env():
    """A context manager: os.environ without HERMETIC_VARS, restored
    in full on exit."""
    patcher = mock.patch.dict(os.environ)

    class _Ctx:
        def __enter__(self):
            patcher.start()
            for name in HERMETIC_VARS:
                os.environ.pop(name, None)
            return os.environ

        def __exit__(self, *exc):
            patcher.stop()
            return False

    return _Ctx()


def install():
    """Make every unittest.TestCase run hermetic. Idempotent."""
    original = unittest.TestCase.run
    if getattr(original, _MARK, False):
        return

    def run(self, result=None):
        with hermetic_env():
            return original(self, result)

    setattr(run, _MARK, True)
    unittest.TestCase.run = run


def installed():
    return bool(getattr(unittest.TestCase.run, _MARK, False))


class HermeticCase(unittest.TestCase):
    """Explicit form of the same guarantee, independent of install()."""

    def setUp(self):
        super().setUp()
        ctx = hermetic_env()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
