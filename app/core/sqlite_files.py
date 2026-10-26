"""Linux metadata handles which do not cancel SQLite's POSIX record locks.

Closing an ordinary descriptor for a database or -shm inode releases SQLite's
locks in the same process. O_PATH descriptors skip that lock cleanup in Linux's
filp_close. Never reopen these handles with os.open, even just to chmod them.
The /proc/self/fd chmod below resolves the pinned inode without opening it.
"""

import ctypes
import os
from pathlib import Path
from uuid import uuid4


# The shipped Linux/glibc runtime supplies renameat2. RENAME_NOREPLACE makes
# publication atomic without ever overwriting an existing database or exposing
# the temporary second link that link()+unlink() would create.
_renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
_renameat2.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
]
_renameat2.restype = ctypes.c_int


def _publish_closed_file(source, destination, *, dir_fd=None):
    parent = -100 if dir_fd is None else dir_fd  # Linux AT_FDCWD
    if _renameat2(parent, os.fsencode(source), parent, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def sqlite_metadata_handle(path, *, create=False, dir_fd=None):
    if create:
        # ctypes char pointers would otherwise silently truncate an embedded NUL.
        if b"\0" in os.fsencode(path):
            raise ValueError("embedded null byte")
        # Close the data descriptor BEFORE publishing the inode at a name SQLite
        # can open. O_CREAT on the database itself would race another thread.
        temporary = str(Path(path).with_name(".sqlite-create-" + uuid4().hex))
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            os.close(descriptor)
            try:
                _publish_closed_file(temporary, path, dir_fd=dir_fd)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except FileNotFoundError:
                pass  # Successful rename already removed the staging name.
    return os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)


def chmod_sqlite_metadata(descriptor, mode):
    os.chmod(f"/proc/self/fd/{descriptor}", mode)
