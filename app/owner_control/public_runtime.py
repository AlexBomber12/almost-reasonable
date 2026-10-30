"""Credential-free, read-only public decisions and finite authority observation."""

import json
from pathlib import Path

from app.core.intellectual_state import VERSION, digest
from app.core.public_behavior import _unique_fields
from app.core.read_only_sqlite import connect_read_only
from app.threads.domain import require_reference
from app.threads.store import SCHEMA_VERSION
from app.trusted.openai_credential_files import read_protected_worker_file


class PublicRuntimeReadView:
    @classmethod
    def from_config(cls, path, *, expected_owner_directory):
        value = json.loads(
            read_protected_worker_file(Path(path), maximum_bytes=4096),
            object_pairs_hook=_unique_fields,
        )
        if (
            type(value) is not dict
            or set(value)
            != {
                "version",
                "account",
                "journal_path",
                "threads_path",
                "ledger_path",
                "owner_directory",
                "scheduler_path",
            }
            or value["version"] != "public-runtime-observation-v1"
        ):
            raise ValueError("invalid public runtime observation config")
        if (
            Path(value["owner_directory"]).resolve()
            != Path(expected_owner_directory).resolve()
        ):
            raise ValueError(
                "public and private applications must share owner Stop store"
            )
        return cls(**{key: field for key, field in value.items() if key != "version"})

    def __init__(
        self,
        *,
        account,
        journal_path,
        threads_path,
        ledger_path,
        owner_directory,
        scheduler_path=None,
    ):
        require_reference("public runtime account", account)
        if any(
            not Path(path).is_absolute()
            for path in (
                journal_path,
                threads_path,
                ledger_path,
                owner_directory,
                scheduler_path,
            )
            if path is not None
        ):
            raise ValueError("public observation paths must be absolute")
        self.account = account
        self.journal, self.threads, self.ledger, self.owner = map(
            Path, (journal_path, threads_path, ledger_path, owner_directory)
        )
        self.scheduler = None if scheduler_path is None else Path(scheduler_path)
        self._schema()

    def _schema(self):
        with connect_read_only(self.journal) as db:
            if db.execute(
                "SELECT version,account FROM public_runtime_schema"
            ).fetchall() != [("threads-public-runtime-v1", self.account)] or db.execute(
                "SELECT version FROM agent_intellectual_schema"
            ).fetchall() != [(VERSION,)]:
                raise ValueError("public runtime observation binding mismatch")
            if db.execute(
                "SELECT value FROM public_runtime_binding WHERE name='owner_directory'"
            ).fetchall() != [(str(self.owner),)]:
                raise ValueError("public owner Stop binding mismatch")
        with connect_read_only(self.threads) as db:
            if db.execute("SELECT version FROM threads_schema_metadata").fetchall() != [
                (SCHEMA_VERSION,)
            ]:
                raise ValueError("public Threads schema mismatch")
        with connect_read_only(self.ledger) as db:
            row = db.execute(
                "SELECT generation FROM privacy_control_generation WHERE singleton=1"
            ).fetchone()
            if row is None or type(row[0]) is not int or row[0] < 0:
                raise ValueError("public control generation unavailable")

    def owner_status(self):
        try:
            self._schema()
            with connect_read_only(self.journal) as db:
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                rows = db.execute(
                    "SELECT operation,phase,payload FROM intents ORDER BY rowid DESC LIMIT 8"
                ).fetchall()
                recent = [
                    {
                        "reference": digest(operation)[:16],
                        "phase": phase,
                        "decision": json.loads(raw).get("decision", "unknown"),
                        "time": json.loads(raw).get("decided_at"),
                    }
                    for operation, phase, raw in rows
                ]
                grants = []
                if "public_grants" in tables:
                    for grant_id, raw, revoked, used in db.execute(
                        "SELECT id,spec,revoked,http_used FROM public_grants ORDER BY rowid DESC LIMIT 4"
                    ):
                        spec = json.loads(raw)
                        reserved = db.execute(
                            "SELECT COUNT(*) FROM public_grant_admissions WHERE grant_id=?",
                            (grant_id,),
                        ).fetchone()[0]
                        grants.append(
                            {
                                "reference": digest(grant_id)[:16],
                                "expires_at": spec["expires_at"],
                                "revoked": bool(revoked),
                                "publications_reserved": reserved,
                                "publication_limit": spec["max_publications"],
                                "http_requests_reserved": used,
                                "http_limit": spec["max_http_requests"],
                            }
                        )
                windows = []
                if "public_windows" in tables:
                    windows = [
                        {
                            "reference": digest(window)[:16],
                            "ticks": ticks,
                            "polls": polls,
                        }
                        for window, ticks, polls in db.execute(
                            "SELECT id,ticks,polls FROM public_windows ORDER BY rowid DESC LIMIT 4"
                        )
                    ]
            cadence = None
            if self.scheduler is not None:
                with connect_read_only(self.scheduler) as db:
                    value = db.execute(
                        "SELECT cadence FROM scheduler_state WHERE singleton=1"
                    ).fetchone()[0]
                cadence = None if value is None else int(value) / 1000000
            return {
                "available": True,
                "scope": "observed_public_runtime",
                "billing_authority": False,
                "recent": recent,
                "grants": grants,
                "windows": windows,
                "cadence_seconds": cadence,
                "cadence_observed": self.scheduler is not None,
            }
        except Exception:
            return {
                "available": False,
                "scope": "observed_public_runtime",
                "billing_authority": False,
            }
