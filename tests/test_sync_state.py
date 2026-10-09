"""cousin-sync-state is deprecated (3.47.0): it says so, writes nothing,
and exits 0, for one release (meeting 11 A)."""
import contextlib
import io
import os
import pathlib
import tempfile
from unittest import mock

from cousin_lib.sync_state import sync_state_main
from tests._hermetic import HermeticCase


class TestDeprecated(HermeticCase):
    def test_it_says_so_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "data").mkdir()
            (home / "STATUS.md").write_text("## Open loops\n- [ ] a loop\n")
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"COUSIN_HOME": str(home)}), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(sync_state_main(["--home", str(home)]), 0)
            self.assertIn("deprecated", err.getvalue())
            self.assertFalse((home / "data" / "state.json").exists())
