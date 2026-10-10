from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
COMPOSE = ROOT / "ops" / "compose.openai-worker.stage1.yaml"
WORKER = ROOT / "ops" / "Dockerfile.openai-worker-stage1"
PLANNER = ROOT / "ops" / "Dockerfile.openai-planner-probe-stage1"
LOCK = ROOT / "ops" / "requirements.openai-worker-stage1.lock"
RUNBOOK = ROOT / "docs" / "openai-worker-stage1-smoke.md"
PRODUCTION_COMPOSE = ROOT / "compose.yaml"
STAGE0_COMPOSE = ROOT / "ops" / "compose.openai-worker.stage0.yaml"


def text(path):
    return path.read_text(encoding="utf-8")


def service_block(compose, service, next_service=None):
    start = compose.index(f"  {service}:")
    end = (
        compose.index(f"  {next_service}:", start)
        if next_service
        else compose.index("\nnetworks:", start)
    )
    return compose[start:end]


def test_stage1_uses_separate_pinned_nonroot_images():
    worker = text(WORKER)
    planner = text(PLANNER)
    base = (
        "FROM python:3.12.12-slim-bookworm@sha256:"
        "593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
    )

    assert worker.startswith(base)
    assert planner.startswith(base)
    assert "USER 11001:11001" in worker
    assert "USER 11002:11002" in planner
    assert "PYTHONDONTWRITEBYTECODE=1" in worker
    assert "PYTHONDONTWRITEBYTECODE=1" in planner
    assert "COPY ." not in worker + planner
    assert "--chmod=0644" in worker + planner
    assert "chmod 0755" in worker + planner


def test_only_worker_image_contains_sdk_and_credential_loader():
    worker = text(WORKER)
    planner = text(PLANNER)
    lock = text(LOCK)

    assert "requirements.openai-worker-stage1.lock" in worker
    assert "openai_credential_files.py" in worker
    assert "openai_stage1_smoke.py" in worker
    assert "pip install" not in planner
    assert "openai_credential_files" not in planner
    assert "openai_transport" not in planner
    assert "openai==3.28.0" in lock
    for line in lock.splitlines():
        assert re.fullmatch(r"[a-z0-9-]+==[A-Za-z0-9.]+", line)


def test_stage1_compose_preserves_uid_mount_and_network_boundaries():
    compose = text(COMPOSE)
    worker = service_block(compose, "openai-worker", "planner-probe")
    planner = service_block(compose, "planner-probe")

    assert "name: almost-reasonable-openai-stage1" in compose
    assert re.findall(r"^  [a-z][a-z0-9-]+:$", compose, re.MULTILINE) == [
        "  openai-worker:",
        "  planner-probe:",
        "  provider-egress:",
    ]
    assert 'user: "${STAGE1_WORKER_UID:-11001}:${STAGE1_WORKER_GID:-11001}"' in worker
    assert (
        'user: "${STAGE1_PLANNER_UID:-11002}:${STAGE1_PLANNER_GID:-11002}"' in planner
    )
    assert "STAGE1_SECRET_DIR" in worker
    assert "target: /run/secrets" in worker
    assert "target: /run/almost-reasonable-stage1/control" in worker
    assert "target: /var/lib/almost-reasonable" in worker
    assert "/run/secrets" not in planner
    assert "/var/lib/almost-reasonable" not in planner
    assert "almost-reasonable-stage1/control" not in planner
    assert "network_mode: none" in planner
    assert "provider-egress" in worker
    assert "read_only: true" in worker
    assert "read_only: true" in planner
    assert compose.count("create_host_path: false") == 5


def test_stage1_services_have_no_ports_privilege_or_docker_socket():
    compose = text(COMPOSE)
    for service in (
        service_block(compose, "openai-worker", "planner-probe"),
        service_block(compose, "planner-probe"),
    ):
        assert "ports:" not in service
        assert "privileged:" not in service
        assert "/var/run/docker.sock" not in service
        assert "no-new-privileges:true" in service
        assert "cap_drop:" in service and "- ALL" in service
        assert "pids_limit:" in service
        assert "mem_limit:" in service
    assert "OPENAI_API_KEY" not in compose


def test_stage1_is_additive_and_does_not_modify_existing_compose_topologies():
    assert "openai-stage1" not in text(PRODUCTION_COMPOSE)
    assert "openai-stage1" not in text(STAGE0_COMPOSE)
    assert "network_mode: none" in text(STAGE0_COMPOSE)


def test_runbook_blocks_live_smoke_on_unverified_egress_and_requires_shutdown():
    runbook = text(RUNBOOK)

    for phase in (
        "Phase A — readiness",
        "Phase B — offline dry run",
        "Phase C — separate explicit operator approval",
        "Phase D — single live request",
        "Phase E — verification and shutdown",
    ):
        assert phase in runbook
    assert "BLOCKED: worker egress not restricted" in runbook
    assert "project-level enforced Hard Spend Limit is USD 10/month" in runbook
    assert "Do not rerun after any result" in runbook
    assert "store=false" in runbook
    assert "tools=[]" in runbook
    assert "host firewall." in runbook
    assert "provider addresses can change" in runbook.lower()
    assert "production Compose, OAuth, Cloudflare, Stage 0" in runbook
