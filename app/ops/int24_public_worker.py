"""Explicit public-only model worker; run under a separate restricted UID."""

import argparse
import json
import os
from pathlib import Path

from app.core.provider_contract import require_identifier
from app.core.provider_spend import ProviderStateStore
from app.core.store import EventStore
from app.integration.threads_stage0 import _identity
from app.integration.threads_runtime import PUBLIC_RUNTIME_VERSION
from app.core.public_behavior import public_context_digest
from app.core.public_experience import inference_context_digest
from app.trusted.openai_credential_files import read_protected_worker_file
from app.trusted.openai_transport import OPENAI_API_BASE_URL
from app.trusted.public_behavior_worker import PublicSDKAdapter, PublicWorkerServer


def _private_config(path):
    file = Path(path).absolute()
    return json.loads(read_protected_worker_file(file, maximum_bytes=8192))


def _billing_scope(config):
    project = config.get("project_id")
    organization = config.get("organization_id")
    try:
        require_identifier("project id", project)
        require_identifier("organization id", organization)
    except ValueError:
        raise ValueError("OpenAI billing scope is invalid") from None
    if not project.startswith("proj_") or not organization.startswith("org-"):
        raise ValueError("OpenAI billing scope is invalid")
    return project, organization


def _attestor(account):
    """Verify the app UID's bounded assertion; never open the app journal."""

    def attest(request):
        value = request.reservation
        if type(value) is not dict or set(value) != {
            "account",
            "kind",
            "event",
            "resumed",
            "runtime_version",
            "phase",
            "context_digest",
            "mission",
            "timeout",
        } | (
            {"intellectual_digest"} if request.intellectual_state is not None else set()
        ):
            return False
        if (
            value["account"] != account
            or value["phase"] != "started"
            or value["runtime_version"] != PUBLIC_RUNTIME_VERSION
            or value["kind"] not in ("wake", "reply", "mention")
            or type(value["event"]) is not str
            or type(value["resumed"]) is not bool
            or type(value["timeout"]) not in (int, float)
        ):
            return False
        expected = (
            _identity(account, "wake", value["event"])
            if value["kind"] == "wake"
            else _identity(
                account,
                value["kind"],
                value["event"],
                "resume" if value.get("resumed") else "initial",
            )
        )
        from app.core.intellectual_state import digest

        return (
            (
                request.intellectual_state is None
                or value["intellectual_digest"] == digest(request.intellectual_state)
            )
            and request.operation_id == expected == request.correlation_id
            and value.get("runtime_version") == PUBLIC_RUNTIME_VERSION
            and request.trigger
            == ("autonomous_wakeup" if value["kind"] == "wake" else "public_message")
            and (
                public_context_digest(request.context)
                if request.experience is None
                else inference_context_digest(request.context, request.experience)
            )
            == value["context_digest"]
            and request.mission == tuple(value["mission"])
            and 0 < request.deadline_seconds <= value["timeout"]
        )

    return attest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = _private_config(args.config)
    if config.get("worker_enabled") is not True:
        raise ValueError("public worker is disabled")
    project, organization = _billing_scope(config)
    from openai import OpenAI

    key = (
        read_protected_worker_file(Path(config["openai_key_file"]), maximum_bytes=512)
        .decode()
        .strip()
    )
    sdk = OpenAI(
        api_key=key,
        max_retries=0,
        timeout=10,
        base_url=OPENAI_API_BASE_URL,
        project=project,
        organization=organization,
    )
    os.umask(0o077)
    server = PublicWorkerServer(
        Path(config["socket_path"]),
        Path(config["attempts_path"]),
        permitted_uid=config["application_uid"],
        socket_gid=config["application_gid"],
        attest=_attestor(config["account_ref"]),
        adapter=PublicSDKAdapter(sdk, model=config["model"]),
        provider_store=ProviderStateStore(
            EventStore(Path(config["worker_event_store_path"]))
        ),
    )
    server.start()
    try:
        while True:
            server.serve_once()
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
