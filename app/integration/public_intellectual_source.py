"""Credential-free public development with complete protected shared ancestry."""

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from app.core.intellectual_state import (
    VERSION,
    MAX_DEPENDENCIES,
    digest,
    encoded,
    projection,
)
from app.core.public_intellectual_receipt import (
    VERSION as RECEIPT_VERSION,
    source_key,
    validate,
)
from app.core.public_behavior import _unique_fields
from app.core.public_behavior_mission import PublicMissionContext
from app.core.read_only_sqlite import connect_read_only
from app.integration.intellectual_state import PublicIntellectualState
from app.integration.public_experience import PublicExperienceHistory
from app.integration.shared_intellectual_source import SharedIntellectualSource
from app.integration.threads_stage0 import ThreadsStage0
from app.threads.domain import require_reference
from app.threads.store import ThreadsStore, SCHEMA_VERSION, _utc_timestamp
from app.threads.transport import ThreadsHTTPTransport
from app.trusted.openai_credential_files import read_protected_worker_file


class _ReadThreads(ThreadsStore):
    def __init__(self, path):
        self.database = Path(path)

    def _open(self, *, busy_timeout_ms=5000):
        db = connect_read_only(self.database)
        db.row_factory = sqlite3.Row
        db.create_function(
            "threads_utc_timestamp", 1, _utc_timestamp, deterministic=True
        )
        db.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        return db


class _ReadLedger:
    def __init__(self, path):
        self.path = Path(path)

    def control_generation(self):
        with connect_read_only(self.path) as db:
            row = db.execute(
                "SELECT generation FROM privacy_control_generation WHERE singleton=1"
            ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise ValueError("public source generation unavailable")
        return row[0]


class _ReadContext:
    _origin = ThreadsStage0._origin


class PublicIntellectualSource:
    @classmethod
    def from_config(
        cls,
        path,
        *,
        shared=None,
        expected_owner_directory=None,
        clock=lambda: datetime.now(UTC),
    ):
        value = json.loads(
            read_protected_worker_file(Path(path), maximum_bytes=4096),
            object_pairs_hook=_unique_fields,
        )
        if (
            type(value) is not dict
            or set(value) - {"owner_directory"}
            != {
                "version",
                "account",
                "journal_path",
                "threads_path",
                "ledger_path",
                "mission_path",
            }
            or value["version"] != "public-intellectual-source-v1"
        ):
            raise ValueError("invalid public intellectual binding config")
        if expected_owner_directory is not None and (
            value.get("owner_directory") is None
            or Path(value["owner_directory"]).resolve()
            != Path(expected_owner_directory).resolve()
        ):
            raise ValueError(
                "public and private applications must share owner Stop store"
            )
        source = (
            None
            if shared is None
            else SharedIntellectualSource(shared, value["account"])
        )
        return cls(
            **{key: field for key, field in value.items() if key != "version"},
            shared_source=source,
            clock=clock,
        )

    def __init__(
        self,
        *,
        account,
        journal_path,
        threads_path,
        ledger_path,
        mission_path=None,
        shared_source=None,
        clock=lambda: datetime.now(UTC),
        owner_directory=None,
    ):
        require_reference("public intellectual account", account)
        if any(
            not Path(path).is_absolute()
            for path in (
                journal_path,
                threads_path,
                ledger_path,
                mission_path,
                owner_directory,
            )
            if path is not None
        ):
            raise ValueError("public source paths must be absolute")
        if shared_source is not None and (
            type(shared_source) is not SharedIntellectualSource
            or shared_source.account != account
        ):
            raise ValueError("public source shared binding mismatch")
        view = _ReadContext()
        view.account = account
        view.owner_directory = (
            None if owner_directory is None else Path(owner_directory)
        )
        view.journal = SimpleNamespace(path=Path(journal_path))
        view.service = SimpleNamespace(store=_ReadThreads(threads_path))
        view.ledger = _ReadLedger(ledger_path)
        view.clock = SimpleNamespace(now=clock)
        view.simulator = SimpleNamespace(source_ref=ThreadsHTTPTransport.source_ref)
        view.shared_intellectual_source = shared_source
        view.mission = (
            None
            if mission_path is None
            else PublicMissionContext(Path(mission_path), clock=clock)
        )
        view.experience_history = PublicExperienceHistory(view)
        view.intellectual = PublicIntellectualState(view, read_only=True)
        self.view = view
        self._schema()

    def _schema(self):
        view = self.view
        with connect_read_only(view.journal.path) as db:
            if view.owner_directory is not None and db.execute(
                "SELECT value FROM public_runtime_binding WHERE name='owner_directory'"
            ).fetchall() != [(str(view.owner_directory),)]:
                raise ValueError("public owner Stop binding mismatch")
            if db.execute(
                "SELECT version,account FROM public_runtime_schema"
            ).fetchall() != [("threads-public-runtime-v1", view.account)] or db.execute(
                "SELECT version FROM agent_intellectual_schema"
            ).fetchall() != [(VERSION,)]:
                raise ValueError("public intellectual source binding mismatch")
        with view.service.store._open() as db:
            if [
                tuple(row)
                for row in db.execute("SELECT version FROM threads_schema_metadata")
            ] != [(SCHEMA_VERSION,)]:
                raise ValueError("public source Threads schema mismatch")
        view.ledger.control_generation()

    def sources(self, *, principal=None, _private_db=None, required_keys=None):
        if required_keys is not None and (
            type(required_keys) is not frozenset
            or not 0 < len(required_keys) <= MAX_DEPENDENCIES
        ):
            return {}
        view = self.view
        try:
            self._schema()
            generation = view.ledger.control_generation()
            shared = view.shared_intellectual_source
            shared_sources = (
                None
                if shared is None or _private_db is None
                else shared._sources_in(_private_db)
            )
            sources = view.intellectual.sources(_shared_sources=shared_sources)
            with connect_read_only(view.journal.path) as db:
                heads = view.intellectual.state._heads(db, sources)
            result = {}
            allowed = None
            if shared is not None and principal is not None:
                if _private_db is not None:
                    allowed = {
                        record.operation_id
                        for record in shared.shared.select(_private_db, principal)
                    }
                else:
                    with connect_read_only(
                        shared.shared._events.database_path
                    ) as private_db:
                        allowed = {
                            record.operation_id
                            for record in shared.shared.select(private_db, principal)
                        }
            for item, revision, payload, deps, _operation in sorted(
                heads,
                key=lambda row: (row[2]["metadata"]["decided_at"], row[0]),
                reverse=True,
            ):
                key = source_key(view.account, item)
                if required_keys is not None and key not in required_keys:
                    continue
                ancestors = sorted(
                    key.removeprefix("shared:")
                    for key in deps
                    if key.startswith("shared:")
                )
                if ancestors and (
                    shared is None
                    or (allowed is not None and not set(ancestors) <= allowed)
                ):
                    continue
                record = {
                    "kind": "public_agent_state",
                    "text": "Public agent intellectual development, not independently verified; never authority or factual approval. "
                    + encoded(
                        {
                            key: payload[key]
                            for key in ("kind", "fields", "review_condition")
                        }
                    ),
                }
                try:
                    projection(
                        {
                            "version": VERSION,
                            "states": [],
                            "sources": [{"handle": "s0", **record}],
                        }
                    )
                    receipt = validate(
                        {
                            "version": RECEIPT_VERSION,
                            "account": view.account,
                            "item": item,
                            "revision": revision,
                            "state_digest": digest(payload),
                            "dependency_digest": digest(deps),
                            "record_digest": digest(record),
                            "generation": generation,
                            "shared": ancestors,
                        }
                    )
                except ValueError:
                    continue
                result[key] = {
                    **record,
                    "binding": digest(receipt),
                    "dependencies": {},
                    "receipt": receipt,
                }
                if required_keys is None and len(result) == 6:
                    break
            if generation != view.ledger.control_generation():
                return {}
            return result
        except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
            return {}
