"""Explicitly started, read-only worker status process; no inference imports."""

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import struct
from app.worker_stop_protocol import STOP_VERSIONS
from time import monotonic

from app.core.provider_contract import MAX_PROVIDER_TOKEN_COUNT, require_identifier
from app.core.provider_spend import CONFIRMED_SPEND_LIMIT_CODES
from app.core.store import EventStore, EventStoreError
from app.owner_control.diagnostics import reference, safe_time
from app.trusted.openai_credential_files import TrustedTransportConfigurationError
from app.trusted.private_pilot import (
    PilotAuthorityError,
    _unique,
    protected_state_identity,
    read_pilot_control,
)
from app.worker_telemetry.protocol import (
    REQUEST,
    TOKEN_KEYS,
    VERSION,
    encode,
    unavailable,
    DIAGNOSTICS_REQUEST,
    DIAGNOSTICS_VERSION,
    MAX_DIAGNOSTICS_RESPONSE,
)

STOP_VERSION = "private-pilot-emergency-stop-v1"


def _diagnostics(path):
    empty = {"available": False, "rows": []}
    connection = None
    try:
        identity = protected_state_identity(path)
        sidecars = _sidecar_identities(path)
        deadline = monotonic() + 0.5
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=0.2, isolation_level=None
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=200")
        connection.set_progress_handler(
            lambda: 1 if monotonic() > deadline else 0, 1000
        )
        connection.execute("BEGIN")
        EventStore._verify_current_schema(connection)
        rows = connection.execute(
            """SELECT started_at, correlation_id, outcome, rejection_reason,
                      total_tokens, cost_estimate_status, 0 AS reservation
               FROM provider_usage_attempts
               UNION ALL
               SELECT r.reserved_at, r.correlation_id, NULL, NULL, NULL, NULL, 1
               FROM provider_attempt_reservations AS r
               WHERE NOT EXISTS (SELECT 1 FROM provider_usage_attempts AS u
                                 WHERE u.attempt_id=r.attempt_id)
               ORDER BY 1 DESC, 2 DESC LIMIT 10"""
        ).fetchall()
        connection.execute("ROLLBACK")
        if protected_state_identity(path) != identity or any(
            before is not None and before != after
            for before, after in zip(sidecars, _sidecar_identities(path), strict=True)
        ):
            raise ValueError("provider source changed")
        result = []
        for time, correlation, outcome, reason, tokens, usage, reservation in rows:
            if reservation:
                category, safe_reason = "reservation", None
            elif outcome == "completed":
                category, safe_reason = "completed", None
            elif outcome == "emergency_stop":
                category, safe_reason = "stopped", None
            elif outcome in {
                "invalid_proposal",
                "invalid_response",
                "incomplete",
                "refused",
                "missing_output",
                "unsupported_output",
                "oversized_output",
                "invalid_request",
            }:
                category = "rejected"
                safe_reason = (
                    "invalid_proposal"
                    if outcome == "invalid_proposal"
                    else "refused"
                    if outcome == "refused"
                    else "invalid_response"
                )
            elif outcome in {"timeout", "connection_error", "persistence_failure"}:
                category, safe_reason = (
                    "uncertain",
                    "timeout" if outcome == "timeout" else "unknown",
                )
            elif outcome in {"authentication_error", "permission_error"}:
                category, safe_reason = "rejected", "authorization"
            elif outcome == "server_error":
                category, safe_reason = "uncertain", "provider_error"
            elif outcome == "rate_limit":
                category, safe_reason = "rate_limited", "rate_limit"
            elif outcome in {
                "spend_suspended",
                "spend_limit_confirmed",
                "billing_quota",
            }:
                category, safe_reason = "stopped", "spend_limit"
            else:
                category, safe_reason = "unknown", "unknown"
            if reason is not None and outcome == "invalid_proposal":
                safe_reason = "invalid_proposal"  # Never echo arbitrary stored reasons.
            if tokens is not None and (
                type(tokens) is not int or not 0 <= tokens <= MAX_PROVIDER_TOKEN_COUNT
            ):
                raise ValueError("invalid token count")
            if usage not in (
                None,
                "estimated",
                "usage_unavailable",
                "usage_incomplete",
                "pricing_unavailable",
            ):
                raise ValueError("invalid usage state")
            if usage in {"usage_unavailable", "usage_incomplete"}:
                safe_reason = (
                    "incomplete_usage" if category == "completed" else safe_reason
                )
            result.append(
                {
                    "time": safe_time(time),
                    "family": "provider_attempt",
                    "outcome": category,
                    "reason": safe_reason,
                    "tokens": str(tokens) if tokens is not None else None,
                    "reference": reference(correlation),
                }
            )
        return {"available": True, "rows": result}
    except (
        OSError,
        sqlite3.Error,
        EventStoreError,
        PilotAuthorityError,
        TypeError,
        ValueError,
    ):
        return empty
    finally:
        if connection is not None:
            connection.close()


def _peer_uid(connection):
    return struct.unpack(
        "iII",
        connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
        ),
    )[1]


def _safe_socket_directory(path, group_gid):
    if not path.is_absolute() or len(os.fsencode(path)) > 107:
        raise ValueError("invalid telemetry socket path")
    parent = path.parent
    try:
        entry = parent.lstat()
        resolved = parent.resolve(strict=True)
    except OSError:
        raise ValueError("telemetry socket directory unavailable") from None
    mode = 0o710 if group_gid is not None else 0o700
    if (
        resolved != parent
        or not stat.S_ISDIR(entry.st_mode)
        or entry.st_uid != os.geteuid()
        or stat.S_IMODE(entry.st_mode) != mode
        or (group_gid is not None and entry.st_gid != group_gid)
    ):
        raise ValueError("unsafe telemetry socket directory")
    if path.exists() or path.is_symlink():
        raise ValueError("telemetry socket already exists")


def _stop(path, stop_version=STOP_VERSION):
    try:
        if stop_version not in STOP_VERSIONS:
            raise ValueError("invalid Stop version")
        value = json.loads(
            read_pilot_control(path, maximum_bytes=1024), object_pairs_hook=_unique
        )
        if (
            type(value) is not dict
            or set(value) != {"version", "stop"}
            or value["version"] != stop_version
            or type(value["stop"]) is not bool
        ):
            raise ValueError("invalid stop control")
        return {"valid": True, "active": value["stop"]}
    except (
        PilotAuthorityError,
        TrustedTransportConfigurationError,
        UnicodeDecodeError,
        ValueError,
        OSError,
    ):
        return {"valid": False, "active": None}


def _sidecar_identities(path):
    identities = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        try:
            entry = sidecar.lstat()
        except FileNotFoundError:
            identities.append(None)
            continue
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.geteuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
            or entry.st_nlink != 1
        ):
            raise ValueError("unsafe database sidecar")
        identities.append((entry.st_dev, entry.st_ino))
    return tuple(identities)


def _valid_stored_time(value):
    try:
        timestamp = datetime.fromisoformat(value)
        return int(
            timestamp.tzinfo is not None
            and EventStore._serialize_time(timestamp) == value
        )
    except (TypeError, ValueError, OverflowError):
        return 0


def _valid_identifier(value):
    try:
        require_identifier("telemetry identifier", value)
    except ValueError:
        return 0
    return 1


def _valid_cost_metadata(status, amount, pricing_version, currency):
    if status == "estimated":
        if (
            amount is None
            or not _valid_identifier(pricing_version)
            or currency != "USD"
        ):
            return 0
        try:
            cost = Decimal(amount)
        except (InvalidOperation, TypeError):
            return 0
        return int(cost.is_finite() and cost >= 0)
    return int(
        status in ("usage_unavailable", "usage_incomplete", "pricing_unavailable")
        and amount is None
        and pricing_version is None
        and currency is None
    )


class _ExactTokenSum:
    def __init__(self):
        self.total = 0

    def step(self, value):
        if value is not None:
            if type(value) is not int:
                raise ValueError("invalid token counter")
            self.total += value

    def finalize(self):
        return str(self.total)


def _provider(path):
    empty = unavailable()["provider"]
    connection = None
    try:
        identity = protected_state_identity(path)
        sidecars = _sidecar_identities(path)
        deadline = monotonic() + 0.5
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=0.2, isolation_level=None
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=200")
        connection.set_progress_handler(
            lambda: 1 if monotonic() > deadline else 0, 1000
        )
        connection.create_function(
            "valid_stored_time", 1, _valid_stored_time, deterministic=True
        )
        connection.create_function(
            "valid_identifier", 1, _valid_identifier, deterministic=True
        )
        connection.create_function(
            "valid_cost_metadata", 4, _valid_cost_metadata, deterministic=True
        )
        connection.create_aggregate("exact_token_sum", 1, _ExactTokenSum)
        connection.execute("BEGIN")
        EventStore._verify_current_schema(connection)
        row = connection.execute(
            """
            SELECT COUNT(*),
                   COALESCE(exact_token_sum(input_tokens),'0'),
                   COALESCE(exact_token_sum(cached_input_tokens),'0'),
                   COALESCE(exact_token_sum(output_tokens),'0'),
                   COALESCE(exact_token_sum(total_tokens),'0'),
                   COALESCE(SUM(CASE WHEN input_tokens IS NULL OR cached_input_tokens IS NULL OR output_tokens IS NULL OR total_tokens IS NULL OR cost_estimate_status IN ('usage_unavailable', 'usage_incomplete') THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN provider != 'openai'
                        OR valid_identifier(attempt_id) != 1
                        OR valid_identifier(correlation_id) != 1
                        OR valid_identifier(model_identifier) != 1
                        OR valid_identifier(provider_config_version) != 1
                        OR valid_identifier(prompt_version) != 1
                        OR valid_identifier(core_self_version) != 1
                        OR valid_identifier(outcome) != 1
                        OR (provider_response_id IS NOT NULL AND valid_identifier(provider_response_id) != 1)
                        OR (provider_error_code IS NOT NULL AND valid_identifier(provider_error_code) != 1)
                        OR (rejection_reason IS NOT NULL AND rejection_reason NOT IN (
                            'incomplete', 'refused', 'missing_output', 'unsupported_output',
                            'oversized_output', 'invalid_response', 'timeout',
                            'invalid_envelope', 'invalid_json', 'conflicting_fields',
                            'invalid_container', 'invalid_decision', 'unknown_decision',
                            'unknown_fields', 'invalid_field', 'oversized_field',
                            'invalid_purpose_proposal'))
                        OR (rejection_reason IS NOT NULL AND outcome NOT IN (
                            'invalid_proposal', 'invalid_response', 'incomplete',
                            'refused', 'missing_output', 'unsupported_output',
                            'oversized_output', 'timeout'))
                        OR valid_stored_time(started_at) != 1
                        OR typeof(duration_ms) != 'integer' OR duration_ms < 0
                        OR typeof(retry_count) != 'integer' OR retry_count < 0
                        OR (input_tokens IS NOT NULL AND (typeof(input_tokens) != 'integer' OR input_tokens < 0 OR input_tokens > ?))
                        OR (cached_input_tokens IS NOT NULL AND (typeof(cached_input_tokens) != 'integer' OR cached_input_tokens < 0 OR cached_input_tokens > ?))
                        OR (output_tokens IS NOT NULL AND (typeof(output_tokens) != 'integer' OR output_tokens < 0 OR output_tokens > ?))
                        OR (total_tokens IS NOT NULL AND (typeof(total_tokens) != 'integer' OR total_tokens < 0 OR total_tokens > ?))
                        OR cached_input_tokens > input_tokens
                        OR (input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND total_tokens IS NOT NULL
                            AND (input_tokens > total_tokens OR output_tokens != total_tokens - input_tokens))
                        OR cost_estimate_status NOT IN ('estimated', 'usage_unavailable', 'usage_incomplete', 'pricing_unavailable')
                        OR valid_cost_metadata(cost_estimate_status, estimated_cost, pricing_version, currency) != 1
                        THEN 1 ELSE 0 END),0)
            FROM provider_usage_attempts
        """,
            (MAX_PROVIDER_TOKEN_COUNT,) * 4,
        ).fetchone()
        row = (row[0], *(int(total) for total in row[1:5]), *row[5:])
        reservations = connection.execute(
            """
            SELECT COUNT(CASE WHEN u.attempt_id IS NULL THEN 1 END),
                   COUNT(CASE WHEN r.provider != 'openai'
                        OR valid_identifier(r.attempt_id) != 1
                        OR valid_identifier(r.correlation_id) != 1
                        OR valid_stored_time(r.reserved_at) != 1
                        OR (u.attempt_id IS NOT NULL AND
                            (u.correlation_id != r.correlation_id OR u.provider != r.provider))
                        THEN 1 END)
            FROM provider_attempt_reservations AS r
            LEFT JOIN provider_usage_attempts AS u ON u.attempt_id = r.attempt_id
            """
        ).fetchone()
        suspensions = connection.execute(
            """SELECT s.provider, s.confirmed_at, s.provider_error_code,
                      s.provider_config_version, s.attempt_id, u.provider,
                      u.started_at, u.provider_error_code,
                      u.provider_config_version, u.outcome
               FROM provider_spend_suspensions AS s
               LEFT JOIN provider_usage_attempts AS u ON u.attempt_id = s.attempt_id
               LIMIT 2"""
        ).fetchall()
        connection.execute("ROLLBACK")
        if (
            row[6]
            or reservations[1]
            or any(type(value) is not int or value < 0 for value in row)
            or len(suspensions) > 1
        ):
            raise ValueError("invalid provider records")
        if suspensions:
            (
                provider,
                confirmed_at,
                code,
                config,
                attempt_id,
                usage_provider,
                started_at,
                usage_code,
                usage_config,
                usage_outcome,
            ) = suspensions[0]
            if (
                provider != "openai"
                or code not in CONFIRMED_SPEND_LIMIT_CODES
                or not _valid_identifier(config)
                or not _valid_identifier(attempt_id)
                or not _valid_stored_time(confirmed_at)
                or (usage_provider, started_at, usage_code, usage_config, usage_outcome)
                != (provider, confirmed_at, code, config, "spend_limit_confirmed")
            ):
                raise ValueError("invalid spend suspension")
        current_sidecars = _sidecar_identities(path)
        if protected_state_identity(path) != identity or any(
            before is not None and before != after
            for before, after in zip(sidecars, current_sidecars, strict=True)
        ):
            raise ValueError("provider source changed")
        return {
            "available": True,
            "attempt_count": row[0],
            "recorded_tokens": dict(zip(TOKEN_KEYS, row[1:5], strict=True)),
            "unknown_usage_attempts": row[5],
            "unresolved_reservations": reservations[0],
            "spend_suspended": bool(suspensions),
        }
    except (
        OSError,
        sqlite3.Error,
        EventStoreError,
        PilotAuthorityError,
        ValueError,
        TypeError,
    ):
        return empty
    finally:
        if connection is not None:
            connection.close()


class WorkerTelemetryServer:
    """One fixed observational operation, with peer authentication first."""

    def __init__(
        self,
        socket_path: Path,
        provider_db: Path,
        stop_path: Path,
        allowed_uids: frozenset[int],
        *,
        socket_group_gid: int | None = None,
        stop_version=STOP_VERSION,
    ):
        if stop_version not in STOP_VERSIONS:
            raise ValueError("invalid worker Stop version")
        self.stop_version = stop_version
        if (
            not hasattr(socket, "SO_PEERCRED")
            or any(
                not isinstance(path, Path) or not path.is_absolute()
                for path in (socket_path, provider_db, stop_path)
            )
            or type(allowed_uids) is not frozenset
            or not allowed_uids
            or any(
                type(uid) is not int or not 0 <= uid <= 2**32 - 2
                for uid in allowed_uids
            )
            or (
                socket_group_gid is not None
                and (
                    type(socket_group_gid) is not int
                    or not 0 <= socket_group_gid <= 2**32 - 2
                )
            )
        ):
            raise ValueError("invalid telemetry configuration")
        self.socket_path, self.provider_db, self.stop_path = (
            socket_path,
            provider_db,
            stop_path,
        )
        self.allowed_uids, self.socket_group_gid = allowed_uids, socket_group_gid
        self.listener = None
        self.identity = None

    def start(self):
        if self.listener is not None:
            raise RuntimeError("telemetry server already started")
        _safe_socket_directory(self.socket_path, self.socket_group_gid)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            listener.bind(str(self.socket_path))
        except OSError:
            listener.close()
            raise RuntimeError("telemetry socket creation failed") from None
        finally:
            os.umask(old_umask)
        try:
            entry = self.socket_path.lstat()
            self.identity = (entry.st_dev, entry.st_ino)
            if self.socket_group_gid is not None:
                os.chown(
                    self.socket_path, -1, self.socket_group_gid, follow_symlinks=False
                )
            mode = 0o660 if self.socket_group_gid is not None else 0o600
            os.chmod(self.socket_path, mode, follow_symlinks=False)
            entry = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(entry.st_mode)
                or (entry.st_dev, entry.st_ino) != self.identity
                or entry.st_uid != os.geteuid()
                or stat.S_IMODE(entry.st_mode) != mode
                or (
                    self.socket_group_gid is not None
                    and entry.st_gid != self.socket_group_gid
                )
            ):
                raise RuntimeError("telemetry socket creation failed")
            listener.listen(8)
            listener.settimeout(0.25)
            self.listener = listener
        except (OSError, RuntimeError):
            listener.close()
            self.close()
            raise RuntimeError("telemetry socket creation failed") from None

    def serve_once(self):
        if self.listener is None:
            raise RuntimeError("telemetry server is not started")
        try:
            connection, _ = self.listener.accept()
        except TimeoutError:
            return False
        with connection:
            connection.settimeout(0.5)
            try:
                if _peer_uid(connection) not in self.allowed_uids:
                    return True
                deadline = monotonic() + 0.5
                request = bytearray()
                maximum_request = max(len(REQUEST), len(DIAGNOSTICS_REQUEST))
                while len(request) <= maximum_request:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError("telemetry request deadline exceeded")
                    connection.settimeout(remaining)
                    chunk = connection.recv(maximum_request + 1 - len(request))
                    if not chunk:
                        break
                    request.extend(chunk)
                if request not in (REQUEST, DIAGNOSTICS_REQUEST):
                    return True
                if request == DIAGNOSTICS_REQUEST:
                    response = {
                        "version": DIAGNOSTICS_VERSION,
                        **_diagnostics(self.provider_db),
                    }
                    connection.sendall(encode(response, MAX_DIAGNOSTICS_RESPONSE))
                    return True
                response = {
                    "version": VERSION,
                    "available": True,
                    "collected_at": datetime.now(UTC).isoformat(),
                    "stop": _stop(self.stop_path, self.stop_version),
                    "provider": _provider(self.provider_db),
                }
                connection.sendall(encode(response))
            except (OSError, TimeoutError, ValueError, struct.error):
                pass
        return True

    def serve(self, *, should_stop=lambda: False):
        try:
            while not should_stop():
                self.serve_once()
        finally:
            self.close()

    def close(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.identity is not None:
            try:
                entry = self.socket_path.lstat()
                if (
                    stat.S_ISSOCK(entry.st_mode)
                    and (entry.st_dev, entry.st_ino) == self.identity
                ):
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass
            self.identity = None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only private worker telemetry")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--provider-db", required=True, type=Path)
    parser.add_argument("--stop-file", required=True, type=Path)
    parser.add_argument("--allow-uid", action="append", required=True, type=int)
    parser.add_argument("--socket-group-gid", type=int)
    parser.add_argument(
        "--stop-version", choices=sorted(STOP_VERSIONS), default=STOP_VERSION
    )
    args = parser.parse_args(argv)
    server = WorkerTelemetryServer(
        args.socket,
        args.provider_db,
        args.stop_file,
        frozenset(args.allow_uid),
        socket_group_gid=args.socket_group_gid,
        stop_version=args.stop_version,
    )
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        server.start()
        server.serve(should_stop=lambda: stopping)
    finally:
        server.close()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
