"""Trusted file-system traversal for privacy control data."""

import os
from pathlib import Path
import stat


def directory_is_trusted(identity: os.stat_result, *, has_descendant: bool) -> bool:
    if identity.st_uid not in {0, os.geteuid()}:
        return False
    writable_by_other_accounts = identity.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if not writable_by_other_accounts:
        return True
    return has_descendant and bool(identity.st_mode & stat.S_ISVTX)


def open_trusted_directory_chain(directory: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in directory.parts[1:]:
            if not directory_is_trusted(os.fstat(descriptor), has_descendant=True):
                raise PermissionError("unsafe directory permissions")
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not directory_is_trusted(os.fstat(descriptor), has_descendant=False):
            raise PermissionError("unsafe directory permissions")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
