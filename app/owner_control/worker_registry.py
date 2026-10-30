"""Explicit bounded registry of secretless worker status and assert-only Stop."""

import json
from pathlib import Path

from app.core.intellectual_state import digest
from app.core.public_behavior import _unique_fields
from app.owner_control.worker_stop import WorkerStopClient
from app.owner_control.worker_telemetry import WorkerTelemetryClient
from app.trusted.openai_credential_files import read_protected_worker_file
from app.worker_telemetry.protocol import unavailable


class WorkerRegistry:
    @classmethod
    def from_config(cls, path):
        value = json.loads(
            read_protected_worker_file(Path(path), maximum_bytes=8192),
            object_pairs_hook=_unique_fields,
        )
        if (
            type(value) is not dict
            or set(value) != {"version", "workers"}
            or value["version"] != "owner-worker-registry-v1"
        ):
            raise ValueError("invalid worker registry config")
        return cls(value["workers"])

    def __init__(self, workers):
        from app.threads.domain import require_reference

        if type(workers) is not list or not 1 <= len(workers) <= 8:
            raise ValueError("worker registry requires 1–8 explicit targets")
        endpoints = []
        for worker in workers:
            if type(worker) is not dict or set(worker) != {
                "name",
                "telemetry_socket",
                "stop_socket",
                "uid",
            }:
                raise ValueError("invalid worker registry target")
            name = worker["name"]
            require_reference("worker name", name)
            if len(name) > 32:
                raise ValueError("worker name too long")
            endpoints.append(
                (
                    name,
                    WorkerTelemetryClient(
                        Path(worker["telemetry_socket"]), worker["uid"]
                    ),
                    WorkerStopClient(Path(worker["stop_socket"]), worker["uid"]),
                )
            )
        endpoints.sort(key=lambda endpoint: endpoint[0])
        self.endpoints = tuple(endpoints)
        self.names = tuple(name for name, _, _ in endpoints)
        if len(set(self.names)) != len(self.names):
            raise ValueError("duplicate worker registry target")
        self.fingerprint = digest(
            [
                [
                    name,
                    str(telemetry.socket_path),
                    str(stop.socket_path),
                    stop.expected_uid,
                ]
                for name, telemetry, stop in endpoints
            ]
        )

    def read(self):
        result = []
        for name, client, _stop in self.endpoints:
            try:
                status = client.read()
            except Exception:
                status = unavailable()
            result.append({"name": name, "status": status})
        return result

    def assert_all(self, previous):
        result = {}
        for name, _telemetry, client in self.endpoints:
            if previous.get(name) == "confirmed":
                result[name] = "confirmed"
                continue
            try:
                outcome = client.assert_stop()
            except Exception:
                outcome = "uncertain"
            result[name] = (
                outcome
                if outcome in ("confirmed", "uncertain", "unavailable")
                else "uncertain"
            )
        return result
