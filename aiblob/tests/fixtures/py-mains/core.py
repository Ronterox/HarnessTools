"""Shared helpers. Select the rows from the table; update the cache from scratch."""
import sqlite3

SCHEMA = """
-- notes and who wrote them
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, author TEXT);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def count(db, author):
    """Run a SELECT from the notes table."""
    g = db.execute("SELECT COUNT(*) FROM notes"
                   " WHERE author = ?", (author,))
    return g.fetchone()[0]


def words(text):
    g = text.split()
    return len(g)
