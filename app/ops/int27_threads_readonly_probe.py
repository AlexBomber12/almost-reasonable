"""Operator-only, bounded Threads read contract observation."""

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
from urllib import parse

from app.privacy.files import open_trusted_directory_chain
from app.threads.domain import InboundKind, require_reference
from app.threads.simulator import ProviderFailure
from app.threads.transport import HTTPResponse, HTTPSClient, ThreadsHTTPTransport
from app.trusted.openai_credential_files import read_protected_worker_file
from app.trusted.openai_transport import TrustedTransportConfigurationError


VERSION = "int27-threads-readonly-probe-v1"
FIELDS = frozenset(
    {
        "version",
        "purpose",
        "reviewed",
        "expires_at",
        "revision",
        "account_ref",
        "provider_user_id",
        "reply_thread_id",
        "streams",
        "owner_directory",
        "token_file",
    }
)
ROW_FIELDS = frozenset(
    {
        "id",
        "text",
        "media_type",
        "timestamp",
        "root_post",
        "replied_to",
        "is_reply_owned_by_me",
    }
)


class ProbeRejected(Exception):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _protected_file(path: Path, maximum: int) -> bytes:
    if not path.is_absolute():
        raise ProbeRejected("configuration_invalid")
    try:
        descriptor = open_trusted_directory_chain(path.parent)
        os.close(descriptor)
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ProbeRejected("configuration_invalid")
        return read_protected_worker_file(path, maximum_bytes=maximum)
    except (OSError, PermissionError, ValueError, TrustedTransportConfigurationError):
        raise ProbeRejected("configuration_invalid") from None


def _config(path: Path, revision: str, now: datetime) -> dict:
    try:
        value = json.loads(_protected_file(path, 8192))
        if type(value) is not dict or set(value) - {"reply_thread_id"} != FIELDS - {
            "reply_thread_id"
        }:
            raise ValueError
        if (
            value["version"] != VERSION
            or value["purpose"] != "threads-read-contract"
            or value["reviewed"] is not True
        ):
            raise ValueError
        if (
            value["revision"] != revision
            or type(value["streams"]) is not list
            or not value["streams"]
        ):
            raise ValueError
        if len(value["streams"]) != len(set(value["streams"])) or not set(
            value["streams"]
        ) <= {"reply", "mention"}:
            raise ValueError
        for field in ("account_ref", "provider_user_id"):
            require_reference(field, value[field])
        value.setdefault("reply_thread_id", None)
        if value["reply_thread_id"] is not None or "reply" in value["streams"]:
            require_reference("reply_thread_id", value["reply_thread_id"])
        for field in ("owner_directory", "token_file"):
            if type(value[field]) is not str or not Path(value[field]).is_absolute():
                raise ValueError
        expires = datetime.fromisoformat(value["expires_at"])
        if expires.tzinfo is None or not now < expires <= now + timedelta(hours=1):
            raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ProbeRejected("configuration_invalid") from None
    return value


def _owner_stopped(directory: Path) -> bool:
    """Read the existing owner Stop from its store without constructing a runtime."""
    try:
        descriptor = open_trusted_directory_chain(directory)
        os.close(descriptor)
        path = directory / "owner.sqlite"
        identity = path.lstat()
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.geteuid()
            or stat.S_IMODE(identity.st_mode) != 0o600
        ):
            return True
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            try:
                sidecar_identity = sidecar.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(sidecar_identity.st_mode)
                or sidecar_identity.st_uid != os.geteuid()
                or stat.S_IMODE(sidecar_identity.st_mode) != 0o600
            ):
                return True
        with sqlite3.connect(
            f"file:{parse.quote(str(path))}?mode=ro", uri=True, timeout=1
        ) as db:
            if db.execute("SELECT version FROM owner_schema").fetchall() != [(1,)]:
                return True
            return db.execute(
                "SELECT active FROM owner_stop WHERE singleton=1"
            ).fetchone() != (0,)
    except (OSError, PermissionError, sqlite3.Error):
        return True


class ReadBoundary:
    def __init__(
        self,
        client,
        config: dict,
        deadline: float,
        monotonic=time.monotonic,
        wall_clock=lambda: datetime.now(UTC),
    ):
        self.client, self.config, self.deadline, self.monotonic, self.wall_clock = (
            client,
            config,
            deadline,
            monotonic,
            wall_clock,
        )
        self.requests = 0
        self.observed = []
        self.last_category = None

    def request(
        self, method: str, url: str, headers: dict, body: bytes | None, timeout: float
    ) -> HTTPResponse:
        c = self.config
        expected = ["https://graph.threads.net/v1.0/me?fields=id"]
        fields = "id,text,media_type,timestamp"
        reply_fields = fields + ",root_post,replied_to,is_reply_owned_by_me"
        for stream in c["streams"]:
            if stream == "reply":
                expected.append(
                    f"https://graph.threads.net/v1.0/{c['reply_thread_id']}/replies?{parse.urlencode({'fields': reply_fields, 'limit': '10', 'reverse': 'false'})}"
                )
            else:
                expected.append(
                    f"https://graph.threads.net/v1.0/{c['provider_user_id']}/mentions?{parse.urlencode({'fields': fields, 'limit': '10'})}"
                )
        if (
            self.requests >= len(expected)
            or method != "GET"
            or body is not None
            or url != expected[self.requests]
        ):
            self.last_category = "request_rejected"
            raise ProbeRejected(self.last_category)
        if _owner_stopped(Path(c["owner_directory"])):
            self.last_category = "owner_stop"
            raise ProbeRejected(self.last_category)
        if self.wall_clock() >= datetime.fromisoformat(c["expires_at"]):
            self.last_category = "configuration_expired"
            raise ProbeRejected(self.last_category)
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            self.last_category = "deadline_exceeded"
            raise ProbeRejected(self.last_category)
        self.requests += 1
        try:
            response = self.client.request(
                method, url, headers, body, min(timeout, remaining)
            )
        except Exception:
            self.last_category = "transport_failure"
            raise ProbeRejected(self.last_category) from None
        if self.monotonic() >= self.deadline:
            self.last_category = "deadline_exceeded"
            raise ProbeRejected(self.last_category)
        if (
            type(response) is not HTTPResponse
            or type(response.body) is not bytes
            or len(response.body) > 2_097_152
        ):
            self.last_category = "response_oversized"
            raise ProbeRejected(self.last_category)
        if response.status in (301, 302, 303, 307, 308):
            self.last_category = "redirect_rejected"
            raise ProbeRejected(self.last_category)
        if response.status in (401, 403):
            self.last_category = "authentication_failure"
            raise ProbeRejected(self.last_category)
        if response.status == 429:
            self.last_category = "rate_limited"
            raise ProbeRejected(self.last_category)
        if response.status != 200:
            self.last_category = "provider_failure"
            raise ProbeRejected(self.last_category)
        try:
            data = json.loads(response.body)
            if type(data) is not dict:
                raise ValueError
            if self.requests == 1:
                if type(data.get("id")) is not str:
                    raise ValueError
                shape = {"operation": "account", "fields": ["id"]}
            else:
                rows = data.get("data")
                if (
                    type(rows) is not list
                    or len(rows) > 10
                    or any(type(row) is not dict for row in rows)
                ):
                    raise ValueError
                shape = {
                    "operation": c["streams"][self.requests - 2],
                    "rows": len(rows),
                    "fields": sorted(
                        set().union(*(set(row) & ROW_FIELDS for row in rows))
                    ),
                }
            self.observed.append(shape)
        except (ValueError, TypeError):
            self.last_category = "contract_malformed"
            raise ProbeRejected(self.last_category) from None
        return response


def run(
    config_path: Path,
    *,
    revision: str,
    client=None,
    clock=lambda: datetime.now(UTC),
    monotonic=time.monotonic,
) -> dict:
    started = clock()
    receipt = {
        "version": VERSION,
        "revision": revision,
        "started_at": started.isoformat(),
        "finished_at": None,
        "configuration_binding": None,
        "requests": 0,
        "observed": [],
        "outcome": "configuration_invalid",
        "unresolved": [
            "app_review",
            "configured_scopes",
            "publishing_permission",
            "semantic_correctness",
            "stage_3_readiness",
        ],
    }
    boundary = None
    deadline = monotonic() + 30
    try:
        c = _config(config_path, revision, started)
        receipt["configuration_binding"] = {
            key: c[key]
            for key in ("account_ref", "provider_user_id", "reply_thread_id", "streams")
        }
        token = _protected_file(Path(c["token_file"]), 512).decode("ascii").strip()
        boundary = ReadBoundary(
            client or HTTPSClient(),
            c,
            deadline,
            monotonic,
            clock,
        )
        transport = ThreadsHTTPTransport(
            account_ref=c["account_ref"],
            provider_user_id=c["provider_user_id"],
            reply_thread_id=c["reply_thread_id"],
            token=token,
            client=boundary,
        )
        transport.verify_account()
        for stream in c["streams"]:
            remaining = boundary.deadline - monotonic()
            if remaining <= 0:
                raise ProbeRejected("deadline_exceeded")
            transport.poll(
                InboundKind(stream),
                None,
                limit=10,
                timeout=min(10, remaining),
            )
        receipt["outcome"] = "observed"
    except ProbeRejected as error:
        receipt["outcome"] = error.category
    except ProviderFailure as error:
        receipt["outcome"] = boundary.last_category or (
            "account_mismatch"
            if error.evidence == "provider_account_mismatch"
            else "contract_malformed"
        )
    except (ValueError, TypeError, UnicodeError):
        receipt["outcome"] = "configuration_invalid"
    receipt["requests"] = boundary.requests if boundary else 0
    receipt["observed"] = boundary.observed if boundary else []
    receipt["finished_at"] = datetime.now(UTC).isoformat()
    return receipt


def _reviewed_revision() -> str:
    """Bind the CLI to its own clean repository before opening authorization."""
    root = Path(__file__).resolve().parents[2]
    command = ["git", "-C", str(root)]
    try:
        top = subprocess.check_output(
            [*command, "rev-parse", "--show-toplevel"], text=True
        ).strip()
        if Path(top).resolve() != root:
            raise ProbeRejected("repository_untrusted")
        status = subprocess.check_output(
            [*command, "status", "--porcelain=v1", "--untracked-files=all"],
            text=True,
        )
        if status:
            raise ProbeRejected("repository_dirty")
        revision = subprocess.check_output(
            [*command, "rev-parse", "HEAD"], text=True
        ).strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ProbeRejected("repository_untrusted")
        return revision
    except (OSError, subprocess.CalledProcessError):
        raise ProbeRejected("repository_untrusted") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Disabled by default Threads read contract probe"
    )
    parser.add_argument(
        "command", nargs="?", choices=("status", "run"), default="status"
    )
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps({"state": "disabled", "requests": 0}))
        return 0
    if args.config is None:
        parser.error("run requires --config")
    try:
        revision = _reviewed_revision()
    except ProbeRejected as error:
        print(
            json.dumps({"state": "blocked", "outcome": error.category, "requests": 0})
        )
        return 1
    receipt = run(args.config, revision=revision)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["outcome"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
