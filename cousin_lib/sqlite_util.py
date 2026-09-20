"""Small SQLite helpers shared by the databases the framework keeps.

The one rule here is that an additive migration has to be safe when two
openers race. `PRAGMA table_info` followed by `ALTER TABLE ADD COLUMN`
is check-then-act: it holds across one connection and nothing else, so
two processes opening the same file both see the column missing, both
alter, and the loser raises. Attempt the alter and treat the duplicate
as success instead: SQLite decides who won, and both callers end up with
the column they wanted.
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
