"""Every cousin flips daily, whether or not its cousin.toml says so.

`flip_at` only ever existed per cousin, and nothing wrote one, so a
cousin spawned or migrated after the operator's one-off pass never
flipped and nothing said so: it simply kept the same session for as
long as nobody looked. A fleet default fixes the class rather than the
instance, and a cousin that really should not flip has to say so.
"""
import pathlib
import tempfile
import unittest

from cousin_lib.config import FrameworkConfig, MissingConfigError, flip_time


def _root(harness=None, cousins=()):
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    (root / "config").mkdir()
    if harness is not None:
        (root / "config" / "harness.toml").write_text(harness)
    for slug, extra in cousins:
        home = root / "cousins" / slug
        home.mkdir(parents=True)
        cousin_extra = extra if not extra.startswith("[") else ""
        tail = extra if extra.startswith("[") else ""
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n%s[chat]\nport = 8100\n%s'
            % (slug, slug.capitalize(), cousin_extra, tail))
    return tmp, root


class TestFlipTime(unittest.TestCase):
    def test_a_cousin_with_no_lifecycle_block_gets_the_default(self):
        tmp, root = _root(cousins=[("wren", "")])
        self.addCleanup(tmp.cleanup)
        cfg = FrameworkConfig(root).list_cousins()[0]
        self.assertEqual(flip_time(cfg, root), "04:00")

    def test_its_own_flip_at_wins(self):
        tmp, root = _root(
            cousins=[("wren", '[lifecycle]\nflip_at = "05:30"\n')])
        self.addCleanup(tmp.cleanup)
        cfg = FrameworkConfig(root).list_cousins()[0]
        self.assertEqual(flip_time(cfg, root), "05:30")

    def test_the_operator_can_move_the_fleet_default(self):
        tmp, root = _root(harness='default_flip_at = "03:15"\n',
                          cousins=[("wren", "")])
        self.addCleanup(tmp.cleanup)
        cfg = FrameworkConfig(root).list_cousins()[0]
        self.assertEqual(flip_time(cfg, root), "03:15")

    def test_never_is_the_explicit_opt_out(self):
        tmp, root = _root(
            cousins=[("wren", '[lifecycle]\nflip_at = "never"\n')])
        self.addCleanup(tmp.cleanup)
        cfg = FrameworkConfig(root).list_cousins()[0]
        self.assertIsNone(flip_time(cfg, root))

    def test_a_worker_never_flips(self):
        tmp, root = _root(cousins=[("job", 'type = "worker"\n')])
        self.addCleanup(tmp.cleanup)
        cfg = FrameworkConfig(root).list_cousins()[0]
        self.assertIsNone(flip_time(cfg, root))

    def test_an_unusable_default_is_loud(self):
        tmp, root = _root(harness='default_flip_at = "half past four"\n',
                          cousins=[("wren", "")])
        self.addCleanup(tmp.cleanup)
        cfg = FrameworkConfig(root).list_cousins()[0]
        with self.assertRaises(MissingConfigError):
            flip_time(cfg, root)


if __name__ == "__main__":
    unittest.main()
