"""One line appended to a JSONL log, whole or not at all for the readers.

A process killed in the middle of an append leaves a line with no
newline. Readers skip a line that is not JSON, but the next append used
to write straight after the fragment, gluing a good entry onto it, so
the reader lost that entry too (#286, point 1). `append_line` starts a
new line when the file does not end with one, and hands the kernel the
whole line in one write(2) on an O_APPEND descriptor, so a later kill
leaves at most that one line torn. runner/stream.py's append keeps the
same rule for the event stream.

It proves atomicity against a kill, not durability against a power
loss: only a caller that asks (`fsync=True`) gets an fsync.

A short write (the disk filled partway, a signal) raises OSError rather
than passing for a landed line; the torn tail it leaves is closed by the
next append's guard."""
import os


def append_line(path, line, *, fsync=False):
    """Append `line` (str, no trailing newline needed) to `path` as one
    line of its own: a newline first when the file is not empty and does
    not end with one, then the line and its newline in one write. OSError
    on a short write; `fsync` syncs the file before returning."""
    data = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    if not data.endswith(b"\n"):
        data += b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size:
            with open(path, "rb") as fh:
                fh.seek(size - 1)
                if fh.read(1) != b"\n":
                    data = b"\n" + data
        written = os.write(fd, data)
        if written != len(data):
            raise OSError("short write to %s: %d of %d bytes" % (path, written, len(data)))
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
