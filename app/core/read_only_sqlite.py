"""Read-only SQLite access never creates a missing source database."""

from pathlib import Path
import sqlite3


def connect_read_only(path):
    return sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)
