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

The same wrapper collects each test's garbage when the test ends. A
socket, database or file a test leaves open is otherwise finalised
whenever the collector next runs, inside some later test, and its
ResourceWarning lands in that test's captured stderr. Collected here,
the warning is reported against the test that leaked. With tracemalloc
on (PYTHONTRACEMALLOC=20, or -X tracemalloc=20) the warning names where
the object was allocated; one allocated in cousin_lib fails the test.
Without tracemalloc there is no telling whose it was, so the warning is
only passed on.
"""
import gc
import os
import pathlib
import tracemalloc
import unittest
import warnings

# Every variable the framework reads to find an install, a cousin, a
# tmux server, a filter override, a supervisor or a new cousin's lane,
# and the framework image's marker (a login line differs inside it).
HERMETIC_VARS = ("FRAMEWORK_ROOT", "COUSIN_HOME", "COUSIN_SLUG",
                 "COUSIN_TMUX_SOCKET", "COUSIN_FILTER_OVERRIDE",
                 "INVOCATION_ID", "COUSIN_SUPERVISED",
                 "COUSIN_DEFAULT_RUNNER", "COUSIN_DEFAULT_ACCOUNT",
                 "COUSIN_IN_CONTAINER")

_MARK = "_cousin_hermetic"


def _restore(snapshot):
    """Put os.environ back to `snapshot` by touching only the keys that
    differ. Never clear-and-refill (what mock.patch.dict does on exit):
    a daemon thread a test left running, or one it is joining, would
    see an empty environment for that moment, and a subprocess it
    starts would get no PATH."""
    for name in [k for k in os.environ if k not in snapshot]:
        os.environ.pop(name, None)
    for name, value in snapshot.items():
        if os.environ.get(name) != value:
            os.environ[name] = value


def hermetic_env():
    """A context manager: os.environ without HERMETIC_VARS, restored
    on exit to exactly what it was on entry."""

    class _Ctx:
        def __enter__(self):
            self.snapshot = dict(os.environ)
            for name in HERMETIC_VARS:
                os.environ.pop(name, None)
            return os.environ

        def __exit__(self, *exc):
            _restore(self.snapshot)
            return False

    return _Ctx()


_PRODUCT = str(pathlib.Path(__file__).resolve().parent.parent / "cousin_lib") + os.sep


def _allocated_in_product(source):
    """The cousin_lib frame an object was allocated in, or None: None too
    when tracemalloc is off or did not see the allocation."""
    if source is None or not tracemalloc.is_tracing():
        return None
    trace = tracemalloc.get_object_traceback(source)
    for frame in reversed(trace or ()):                  # innermost first
        if frame.filename.startswith(_PRODUCT):
            return "%s:%d" % (frame.filename[len(_PRODUCT) - len("cousin_lib/"):],
                              frame.lineno)
    return None


def collect_leaks(test_id=None):
    """Run the collector and return the ResourceWarnings it raised, as
    (message, cousin_lib allocation site or None). Every warning not
    returned as a cousin_lib leak is passed on, a ResourceWarning with
    the id of the test that left the object behind."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gc.collect()
    leaks = []
    for w in caught:
        site = None
        if issubclass(w.category, ResourceWarning):
            site = _allocated_in_product(w.source)
            leaks.append((str(w.message), site))
        if site is None:
            message = w.message
            if issubclass(w.category, ResourceWarning) and test_id:
                message = ResourceWarning("%s, left open by %s" % (message, test_id))
            warnings.warn_explicit(message, w.category, w.filename, w.lineno,
                                   source=w.source)
    return leaks


def _fail_on_product_leaks(test, result):
    ours = [(msg, site) for msg, site in collect_leaks(test.id()) if site]
    if ours and result is not None:
        lines = "\n".join("  %s, allocated at %s" % pair for pair in ours)
        err = AssertionError("left open by cousin_lib, found when the test ended:\n" + lines)
        result.addFailure(test, (AssertionError, err, None))


def install():
    """Make every unittest.TestCase run hermetic. Idempotent."""
    original = unittest.TestCase.run
    if getattr(original, _MARK, False):
        return

    def run(self, result=None):
        with hermetic_env():
            result = original(self, result)
        _fail_on_product_leaks(self, result)
        return result

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
