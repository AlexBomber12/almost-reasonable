"""Read-only SQLite scopes never create sources or defer closing their files."""

from pathlib import Path
import sqlite3


class _ScopedReadConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect_read_only(path):
    return sqlite3.connect(
        Path(path).absolute().as_uri() + "?mode=ro",
        uri=True,
        factory=_ScopedReadConnection,
    )
