"""Whole-file writes a kill never leaves half done (#294).

`write_text(path, text)` writes the text to a temp file beside `path`
(a unique name, so two writers never share one), then `os.replace` puts
it over `path` in one step. A process killed before the rename leaves
`path` exactly as it was, plus a temp file named `.<name>.<random>.tmp`
that nothing reads. A symlinked `path` is written through: its target
is replaced, the link stays.

The rename makes a new file: a hard link to the old one keeps the old
text, the file belongs to the writer, extended attributes and ACLs are
not carried over, and the directory must be writable. Right for the
files a home owns; not for a file shared by link.

No fsync: a SIGKILL cannot tear a renamed file (the page cache survives
it); a power loss can, and that is a tier of its own (#286)."""
import os
import tempfile
from pathlib import Path

from cousin_lib.crashpoint import crashpoint


def _umask():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    return 0o022


def write_text(path, text, *, mode=None, newline=None, encoding="utf-8"):
    """Replace `path` with `text` in one step. `mode` sets the file's
    permissions; by default an existing file keeps its own and a new one
    gets what a plain write would (0666 less the umask)."""
    path = Path(path)
    if path.is_symlink():
        path = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".%s." % path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as fh:
            fh.write(text)
        if mode is None:
            try:
                mode = path.stat().st_mode & 0o7777
            except FileNotFoundError:
                mode = 0o666 & ~_umask()
        os.chmod(tmp, mode)
        crashpoint("atomic.written")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
