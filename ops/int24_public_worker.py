"""Explicit public-only model worker; run under a separate restricted UID."""

import argparse
import json
import os
from pathlib import Path
import sqlite3

from app.core.provider_contract import require_identifier
from app.integration.threads_stage0 import _identity
from app.integration.threads_runtime import PUBLIC_RUNTIME_VERSION
from app.core.public_behavior import public_context_digest
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


def _attestor(path, account):
    def attest(request):
        try:
            with sqlite3.connect(
                Path(path).absolute().as_uri() + "?mode=ro", uri=True
            ) as db:
                marker = db.execute(
                    "SELECT version,account FROM public_runtime_schema"
                ).fetchall()
                row = db.execute(
                    "SELECT phase,payload FROM intents WHERE operation=?",
                    (request.operation_id,),
                ).fetchone()
        except sqlite3.Error:
            return False
        if (
            marker != [(PUBLIC_RUNTIME_VERSION, account)]
            or row is None
            or row[0] != "started"
        ):
            return False
        value = json.loads(row[1])
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
        return (
            request.operation_id == expected == request.correlation_id
            and value.get("runtime_version") == PUBLIC_RUNTIME_VERSION
            and request.trigger == value["trigger"]
            and public_context_digest(request.context) == value["context_digest"]
            and request.mission == tuple(value["mission_text"])
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
        attest=_attestor(config["journal_path"], config["account_ref"]),
        adapter=PublicSDKAdapter(sdk, model=config["model"]),
    )
    server.start()
    try:
        while True:
            server.serve_once()
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
