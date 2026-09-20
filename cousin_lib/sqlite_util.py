"""Small SQLite helpers shared by the databases the framework keeps.

The one rule here is that an additive migration has to be safe when two
openers race. `PRAGMA table_info` followed by `ALTER TABLE ADD COLUMN`
is check-then-act: it holds across one connection and nothing else, so
two processes opening the same file both see the column missing, both
alter, and the loser raises. Attempt the alter and treat the duplicate
as success instead: SQLite decides who won, and both callers end up with
the column they wanted.

Attempting it every time is also the cheaper half, which is not obvious.
A duplicate ALTER fails while the statement is being prepared, before it
reaches the locking stage, so it takes no write lock: measured against a
connection holding RESERVED, it returns "duplicate column name" in under
a millisecond while a new-column ALTER and a plain INSERT both wait out
the busy timeout and fail "database is locked". Uncontended it is also
the cheaper of the two, by a factor that four runs put anywhere between
2x and 6x: the duplicate ALTER under 15 us against 35-50 us for the
PRAGMA read it replaces. Tens of microseconds do not carry a precise
ratio, and whichever of the two runs first pays the warm-up, so take the
direction and not the number. That matters because jobs._db() re-runs its
migration on every call: were the failed DDL to take the lock, every read
of that database would become a writer.
"""
import sqlite3

_DUPLICATE = "duplicate column name"


def add_column(conn, table, name, decl):
    """Add one column unless it is already there, safely against another
    connection adding it at the same moment. Any other error is the
    caller's to handle and is raised."""
    try:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
    except sqlite3.OperationalError as err:
        if _DUPLICATE not in str(err).lower():
            raise


def add_columns(conn, table, columns):
    """add_column for a {name: decl} mapping, in order."""
    for name, decl in columns.items():
        add_column(conn, table, name, decl)


def wal(conn):
    """Put this database in WAL mode, where a writer does not block
    readers. Persistent per file, so the first writer converts it and
    later opens are a no-op. Best-effort: WAL is unavailable on a
    network filesystem, and a database that works in the rollback
    journal is better than a refusal to open. Never inside a
    transaction, so call it right after connecting."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn
