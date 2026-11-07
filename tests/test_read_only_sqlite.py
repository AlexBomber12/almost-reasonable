"""Read-only scopes release files without waiting for cyclic garbage collection."""

from contextlib import nullcontext
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.core.read_only_sqlite import connect_read_only


@pytest.mark.parametrize("body_error", [False, True])
def test_read_only_scope_closes_on_success_and_error(tmp_path, body_error):
    path = tmp_path / "source.sqlite"
    sqlite3.connect(path).close()
    with pytest.raises(ValueError) if body_error else nullcontext():
        with connect_read_only(path) as db:
            assert db.execute("SELECT 1").fetchone() == (1,)
            if body_error:
                raise ValueError("synthetic read failure")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


def test_repeated_reads_fit_a_small_file_limit_without_garbage_collection(tmp_path):
    path = tmp_path / "source.sqlite"
    sqlite3.connect(path).close()
    code = """
import gc, resource, sys
from app.core.read_only_sqlite import connect_read_only
resource.setrlimit(resource.RLIMIT_NOFILE, (64, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
gc.disable()
for _ in range(128):
    with connect_read_only(sys.argv[1]) as db:
        assert db.execute('SELECT 1').fetchone() == (1,)
print('128 reads completed')
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    }
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "128 reads completed"
