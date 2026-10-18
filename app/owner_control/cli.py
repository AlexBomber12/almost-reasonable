"""Provision only from an authenticated local OS operator shell."""

import argparse
from getpass import getpass, GetPassWarning
from pathlib import Path
import warnings

from .store import OwnerStore


def main(argv=None, *, password_reader=getpass):
    parser = argparse.ArgumentParser(description="Provision a local owner account")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--rotate-password", action="store_true")
    args = parser.parse_args(argv)
    if args.rotate_password and not (Path(args.directory) / "owner.sqlite").is_file():
        raise SystemExit("Password rotation requires an existing owner store.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = password_reader(
                "New owner password: " if args.rotate_password else "Owner password: "
            )
            confirmation = password_reader("Confirm owner password: ")
    except GetPassWarning:
        raise SystemExit("Interactive hidden password input is required.") from None
    if password != confirmation:
        raise SystemExit("password confirmation does not match")
    if args.rotate_password:
        try:
            OwnerStore(args.directory).rotate_password(args.username, password)
        except (ValueError, RuntimeError, PermissionError):
            raise SystemExit("Owner credential update failed.") from None
        print("Owner password rotated; previous sessions revoked.")
    else:
        OwnerStore(args.directory).provision(args.username, password)
    return 0
