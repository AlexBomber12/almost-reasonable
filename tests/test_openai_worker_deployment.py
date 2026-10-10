from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
COMPOSE = ROOT / "ops" / "compose.openai-worker.stage0.yaml"
DOCKERFILE = ROOT / "ops" / "Dockerfile.openai-worker-stage0"


def deployment_text(path):
    return path.read_text(encoding="utf-8")


def service_block(compose, service, next_service=None):
    start = compose.index(f"  {service}:")
    end = compose.index(f"  {next_service}:", start) if next_service else len(compose)
    return compose[start:end]


def test_stage0_image_is_pinned_minimal_nonroot_and_bytecode_free():
    dockerfile = deployment_text(DOCKERFILE)

    assert re.search(
        r"^FROM python:3\.12\.12-slim-bookworm@sha256:[0-9a-f]{64}$",
        dockerfile,
        re.MULTILINE,
    )
    assert "USER 11001:11001" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "COPY ." not in dockerfile
    assert "apt-get" not in dockerfile
    assert "pip install" not in dockerfile
    assert "OPENAI_API_KEY" not in dockerfile


def test_stage0_compose_has_only_isolated_worker_and_probe():
    compose = deployment_text(COMPOSE)
    worker = service_block(compose, "openai-worker", "planner-probe")
    probe = service_block(compose, "planner-probe")

    assert "name: almost-reasonable-openai-stage0" in compose
    assert re.findall(r"^  [a-z][a-z0-9-]+:$", compose, re.MULTILINE) == [
        "  openai-worker:",
        "  planner-probe:",
    ]
    for service in (worker, probe):
        assert "network_mode: none" in service
        assert "read_only: true" in service
        assert "no-new-privileges:true" in service
        assert "cap_drop:" in service and "- ALL" in service
        assert "pids_limit:" in service
        assert "mem_limit:" in service
        assert "ports:" not in service
        assert "/var/run/docker.sock" not in service
        assert "OPENAI_API_KEY" not in service
        assert "privileged:" not in service

    assert 'user: "${STAGE0_WORKER_UID:-11001}:${STAGE0_WORKER_GID:-11001}"' in worker
    assert 'user: "${STAGE0_PLANNER_UID:-11002}:${STAGE0_PLANNER_GID:-11002}"' in probe
    assert "--socket-group-gid" in worker
    assert "app.trusted.openai_worker_server" in worker
    assert "target: /var/lib/almost-reasonable" in worker
    assert "/var/lib/almost-reasonable" not in probe
    assert (
        "target: /run/almost-reasonable\n        read_only: true\n        bind:"
        in probe
    )
    assert "restart:" not in compose


def test_stage0_bind_mounts_require_precreated_host_directories():
    compose = deployment_text(COMPOSE)

    assert compose.count("create_host_path: false") == 3
    assert "STAGE0_SOCKET_DIR" in compose
    assert "STAGE0_DATABASE_DIR" in compose
    assert "secrets/" not in compose
    assert "compose.yaml" not in compose


def test_dockerignore_is_an_explicit_allowlist_and_excludes_sensitive_state():
    ignored = deployment_text(ROOT / ".dockerignore")

    assert ignored.startswith("**\n")
    for exclusion in (
        "secrets/",
        "data/",
        ".env*",
        ".git/",
        "*.sqlite*",
        "*.log",
        "__pycache__/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".coverage*",
        "*~",
        "*.bak",
    ):
        assert exclusion in ignored
    assert "!app/core/openai_worker_client.py" in ignored
    assert "!app/trusted/openai_worker_server.py" in ignored
    assert "!app/ops/openai_worker_probe.py" in ignored
    assert "!app/core/self_state.py" not in ignored
