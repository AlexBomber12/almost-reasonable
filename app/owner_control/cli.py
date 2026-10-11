"""Provision only from an authenticated local OS operator shell."""

import argparse
from getpass import getpass

from .store import OwnerStore


def main(argv=None, *, password_reader=getpass):
    parser = argparse.ArgumentParser(description="Provision a local owner account")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    password = password_reader("Owner password: ")
    if password != password_reader("Confirm owner password: "):
        raise SystemExit("password confirmation does not match")
    OwnerStore(args.directory).provision(args.username, password)
    return 0
