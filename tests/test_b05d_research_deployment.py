"""Shipped Compose, production identities, real chat/control IPC, fake providers."""

import inspect
import json
from pathlib import Path
import socket

import pytest

from app.ops.private_research_config import extension_command
from test_b05a_profile import QUESTIONS
from test_b05c_claim_evidence import assessed_raw
from test_b09d_owner_deployment import (  # reuse the disposable Docker lifecycle
    ROOT,
    SEED,
    container_image,  # noqa: F401 -- pytest fixture
    deployment,  # noqa: F401 -- pytest fixture
    eventually,
    run,
)
from test_openai_transport import documented_response
from test_participant_privacy_http import acknowledge, post
from test_private_web_http import Browser


# Only credential construction is replaced. Production CLI, gateway, private
# allowance, research journal, adapters, sockets and SQLite remain in the path.
LAUNCH = r"""
import importlib, json, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, "/opt/almost-reasonable")
from app.trusted.openai_transport import OpenAIResponsesTransport
from app.trusted.public_research_transport import PublicResearchTransport
module = sys.argv[4]
fixture = json.loads(Path('/test/payloads.json').read_text())
class Responses:
    def create(self, **kwargs):
        name = 'research' if module.endswith('public_research_worker') else 'inference'
        log = Path('/var/lib/pilot-worker') / (name + '-calls.jsonl')
        with log.open('a') as output:
            output.write(json.dumps(kwargs) + '\n')
            output.flush()
            os.fsync(output.fileno())
        if name == 'research' and len(log.read_text().splitlines()) == 2:
            os._exit(23)  # lost response after durable reservation/contact
        return fixture[name]
client = SimpleNamespace(responses=Responses())
target = importlib.import_module(module)
if module.endswith('private_pilot_worker'):
    target.build_worker_openai_transport = lambda **kw: OpenAIResponsesTransport(client)
else:
    target.PublicResearchTransport.from_protected_file = lambda **kw: PublicResearchTransport(client)
raise SystemExit(target.main(sys.argv[5:]))
"""

EXTRA_SEED = r"""
from datetime import timedelta
from app.private_web.auth import AuthStore, _account_source_reference
from app.core.public_research_profile import provider_configuration
from app.trusted.private_pilot import PilotAllowance, load_activation
now = datetime.now(UTC).replace(microsecond=0)
os.setegid(11022)
os.seteuid(11022)
AuthStore(a / 'auth.db').provision_account('alice','synthetic-password-alice','participant-alice')
os.seteuid(0)
os.setegid(0)
for name in ('ipc', 'research-ipc', 'research-app', 'research-worker', 'secrets'):
    (p / name).mkdir(mode=0o700)
activation = {
    'version':'private-pilot-activation-v1', 'pilot_id':'synthetic-b05d',
    'application_peer_uid':11022, 'provider_config_version':'openai-private-pilot-v1',
    'model':'gpt-6-luna', 'project_id':'proj_synthetic', 'organization_id':'org-synthetic',
    'not_before':(now-timedelta(seconds=5)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'expires_at':(now+timedelta(seconds=590)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'max_operations':5, 'max_attempts_per_operation':1,
}
activation_path = p / 'control/activation.json'
activation_path.write_text(json.dumps(activation))
activation_path.chmod(0o400)
profile = {
    'version':'public-research-profile-v1',
    'purpose':'private-conversation-public-research-v1', 'configuration':'public-research-v1',
    'provider_configuration':provider_configuration(), 'profile_id':'a'*64,
    'authorization':'b'*64, 'application_scope':'test-private-chat', 'peer_uid':11022,
    'issued_at':now.isoformat(), 'reviewed_at':now.isoformat(),
    'expires_at':(now+timedelta(days=1)).isoformat(), 'reviewer':'synthetic-operator',
    'aggregate_limit':2, 'topics':[{'id':'ocean','question':'How do deep ocean currents form?','limit':2}],
}
(p / 'research-worker/profile.json').write_text(json.dumps({**profile,'role':'worker'}))
(p / 'research-app/profile.json').write_text(json.dumps({
    **profile,'role':'application', 'principal_id':'participant-alice',
    'authenticated_source_ref':_account_source_reference('participant-alice'),
}))
(p / 'worker/research-authority.sqlite3').touch(mode=0o600)
os.chown(p / 'worker/research-authority.sqlite3',11021,11021)
os.chown(activation_path,11021,11021)
# One-time disposable fixture preparation under the actual worker identity.
os.setegid(11021)
os.seteuid(11021)
PilotAllowance.initialize(p / 'worker/historical.sqlite3',load_activation(activation_path))
os.seteuid(0)
os.setegid(0)
for directory, uid, gid, mode in (
    ('ipc',11021,11020,0o710), ('research-ipc',11021,11020,0o710),
    ('research-app',11022,11022,0o700), ('research-worker',11021,11021,0o700),
):
    for entry in (p / directory).iterdir():
        entry.chmod(0o600)
        os.chown(entry,uid,gid)
    (p / directory).chmod(mode)
    os.chown(p / directory,uid,gid)
"""


@pytest.fixture
def research_stack(deployment, container_image):  # noqa: F811 -- imported pytest fixtures
    root, env, owner_command = deployment
    # Use daemon-native state for concurrent SQLite WAL and IPC, including on
    # Docker Desktop. Shipped bind mounts are verified before fixture overrides.
    volume = run("docker", "volume", "create", owner_command[3] + "-research-state")
    native = run("docker", "volume", "inspect", "--format", "{{.Mountpoint}}", volume)
    root.chmod(0o755)
    for key, directory in {
        "PILOT_APP_STATE_DIR": "app",
        "PILOT_WORKER_STATE_DIR": "worker",
        "PILOT_CONTROL_DIR": "control",
        "PILOT_TELEMETRY_SOCKET_DIR": "telemetry",
        "PILOT_STOP_SOCKET_DIR": "stop-ipc",
    }.items():
        env[key] = native + "/" + directory
    env.update(
        {
            "PILOT_RESEARCH_APP_IMAGE": container_image,
            "PILOT_RESEARCH_WORKER_IMAGE": container_image,
            "PILOT_RESEARCH_SOCKET_DIR": native + "/research-ipc",
            "PILOT_RESEARCH_APP_PROFILE_DIR": native + "/research-app",
            "PILOT_RESEARCH_WORKER_PROFILE_DIR": native + "/research-worker",
            "PILOT_RESEARCH_PROJECT_ID": "proj_synthetic",
            "PILOT_RESEARCH_ORGANIZATION_ID": "org-synthetic",
            "PILOT_SOCKET_DIR": native + "/ipc",
            "PILOT_SECRET_DIR": native + "/secrets",
            "PILOT_ALLOWED_HOST": "chat.test",
            "PILOT_ALLOWED_ORIGIN": "https://chat.test",
        }
    )
    (root / "deployment.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    pilot = owner_command[:-2] + ["-f", str(ROOT / "ops/compose.private-pilot.yaml")]
    protected = root / "protected.json"
    config = json.loads(run(*pilot, "config", "--format", "json"))
    worker = config["services"]["provider-worker"]["command"]
    worker[worker.index("--allowance-database") + 1] = (
        "/var/lib/pilot-worker/historical.sqlite3"
    )
    protected.write_text(
        json.dumps(
            {
                "services": {
                    "provider-worker": {"command": worker},
                    "private-app": {"image": container_image},
                }
            }
        )
    )
    pilot += ["-f", str(protected)]
    original_pilot = list(pilot)
    before = json.loads(run(*pilot, "config", "--format", "json"))
    generated = root / "app-command.json"
    generated.write_text(json.dumps(extension_command(before, "test-private-chat")))
    pilot += [
        "-f",
        str(ROOT / "ops/compose.private-research.yaml"),
        "-f",
        str(generated),
    ]
    after = json.loads(run(*pilot, "config", "--format", "json"))
    assert after["services"]["provider-worker"] == before["services"]["provider-worker"]
    assert (
        after["services"]["private-app"]["networks"]
        == before["services"]["private-app"]["networks"]
    )
    assert (
        after["services"]["private-app"]["ports"]
        == before["services"]["private-app"]["ports"]
    )
    research = after["services"]["research-worker"]
    assert research["user"] == "11021:11021" and research["group_add"] == ["11020"]
    assert research["networks"] == after["services"]["provider-worker"]["networks"]
    assert research["cap_drop"] == ["ALL"] and research["read_only"]
    assert research["security_opt"] == ["no-new-privileges:true"]
    assert not research.get("ports") and not research.get("depends_on")
    for service in ("private-app", "research-worker"):
        for mount in after["services"][service]["volumes"]:
            assert not mount.get("bind", {}).get("create_host_path", False)
    app_mounts = {v["target"] for v in after["services"]["private-app"]["volumes"]}
    assert (
        "/run/secrets" not in app_mounts and "/var/lib/pilot-worker" not in app_mounts
    )
    assert "/run/almost-reasonable-private-pilot/control" not in app_mounts
    (root / "launch.py").write_text(LAUNCH)
    inference = documented_response()
    inference["output"][1]["content"][0]["text"] = json.dumps(
        {"proposal": {"decision": "research", "brief": "ocean"}}
    )
    research_raw = assessed_raw()
    research_raw["id"] = "resp_" + "a" * 48
    research_raw["output"][0]["id"] = "ws_" + "b" * 48
    research_raw["output"][1]["id"] = "msg_" + "c" * 48
    research_raw["usage"] = {
        "input_tokens": 30,
        "output_tokens": 10,
        "total_tokens": 40,
    }
    (root / "payloads.json").write_text(
        json.dumps({"inference": inference, "research": research_raw})
    )
    # Test-only isolation: no egress, no published participant port, and only
    # credential factories replaced. Every shipped command argument is retained.
    testing = root / "offline.yaml"
    testing.write_text(f"""
services:
  private-app:
    ports: !reset []
    networks: !reset []
    network_mode: none
  provider-worker:
    image: {container_image}
    networks: !reset []
    network_mode: none
    entrypoint: [python, -B, /test/launch.py]
    volumes:
      - type: bind
        source: {root}
        target: /test
        read_only: true
        bind: {{create_host_path: false}}
  research-worker:
    networks: !reset []
    network_mode: none
    entrypoint: [python, -B, /test/launch.py]
    volumes:
      - type: bind
        source: {root}
        target: /test
        read_only: true
        bind: {{create_host_path: false}}
""")
    pilot += ["-f", str(testing)]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    owner_config = json.loads(run(*owner_command, "config", "--format", "json"))
    owner_args = owner_config["services"]["owner-console"]["command"]
    owner_args[owner_args.index("--port") + 1] = str(port)
    owner_port = root / "owner-port.json"
    owner_port.write_text(
        json.dumps({"services": {"owner-console": {"command": owner_args}}})
    )
    # One test project may include the otherwise separate B09D services safely.
    combined = pilot + [
        "-f",
        str(ROOT / "ops/compose.owner-control.yaml"),
        "-f",
        str(owner_port),
    ]

    # Owner file has its own name; explicit --project-name already selects the fixture project.
    def native_mounts(mounts):
        return [
            {
                "type": "volume",
                "source": "runtime-state",
                "target": mount["target"],
                "read_only": mount.get("read_only", False),
                "volume": {"subpath": mount["source"].removeprefix(native + "/")},
            }
            if mount["source"].startswith(native + "/")
            else mount
            for mount in mounts
        ]

    volume_header = (
        "volumes:\n  runtime-state:\n    external: true\n    name: "
        + volume
        + "\nservices:\n"
    )
    runtime_model = json.loads(run(*combined, "config", "--format", "json"))
    native_override = root / "native-state.yaml"
    native_override.write_text(
        volume_header
        + "".join(
            "  "
            + name
            + ":\n    environment: {PYTHONFAULTHANDLER: '1'}\n    volumes: !override "
            + json.dumps(native_mounts(service["volumes"]))
            + "\n"
            for name, service in runtime_model["services"].items()
        )
    )
    combined += ["-f", str(native_override)]
    rollback = root / "rollback.yaml"
    rollback.write_text(
        volume_header
        + "  private-app:\n    ports: !reset []\n    networks: !reset []\n    network_mode: none\n    volumes: !override "
        + json.dumps(native_mounts(before["services"]["private-app"]["volumes"]))
        + "\n"
    )
    extra_seed = SEED.replace(
        "(p / 'ready').touch()",
        EXTRA_SEED + "\nconnection.close()\nconnection = None\n(p / 'ready').touch()",
    )
    # Allow the fixture's initialization to drop and regain synthetic numeric IDs.
    seed = run(
        "docker",
        "run",
        "-d",
        "--network",
        "none",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "-v",
        f"{volume}:/seed",
        "--entrypoint",
        "python",
        container_image,
        "-B",
        "-c",
        extra_seed,
    )
    try:
        try:
            eventually(
                lambda: run(
                    "docker",
                    "exec",
                    seed,
                    "python",
                    "-c",
                    "from pathlib import Path; print(Path('/seed/ready').exists())",
                    check=False,
                )
                == "True"
            )
        except BaseException:
            pytest.fail(run("docker", "logs", seed))
        # The ordinary deployment already has the independent B09D controls.
        # Start and admit each store user before the next, matching the runbook.
        for services, service, probe in (
            (
                ("worker-telemetry", "worker-stop", "owner-console"),
                "owner-console",
                f"import http.client; c=http.client.HTTPConnection('127.0.0.1',{port}); c.request('GET','/login',headers={{'Host':'owner.test','X-Forwarded-For':'127.0.0.1'}}); print(c.getresponse().status)",
            ),
            (
                ("provider-worker",),
                "provider-worker",
                "from pathlib import Path; print(Path('/run/pilot-ipc/worker.sock').is_socket())",
            ),
            (
                ("research-worker",),
                "research-worker",
                "from pathlib import Path; print(Path('/run/pilot-research-ipc/research.sock').is_socket())",
            ),
            (
                ("private-app",),
                "private-app",
                "import http.client; c=http.client.HTTPConnection('127.0.0.1',8000); c.request('GET','/login',headers={'Host':'chat.test'}); print(c.getresponse().status)",
            ),
        ):
            run(
                *combined,
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                *services,
            )
            eventually(
                lambda: run(
                    *combined, "exec", "-T", service, "python", "-c", probe, check=False
                )
                in ("True", "200")
            )
        yield root, combined, seed, port, original_pilot + ["-f", str(rollback)]
    except BaseException:
        print(run(*combined, "logs", check=False))
        raise
    finally:
        print(run(*combined, "logs", check=False))
        run(*combined, "down", "--timeout", "3", check=False)
        run("docker", "rm", "-f", seed, check=False)
        run("docker", "volume", "rm", volume, check=False)


def test_connected_research_usage_lost_response_restart_stop_and_revocation(
    research_stack,
):
    root, command, seed, port, original_pilot = research_stack

    def execute(service, code):
        return run(*command, "exec", "-T", service, "python", "-B", "-c", code)

    def inspect_seed(code):
        return run("docker", "exec", seed, "python", "-B", "-c", code)

    def chat(action):
        # Reuse the existing Browser's authentication/CSRF/replay behavior, with
        # a real HTTP adapter in the app's isolated network namespace.
        return json.loads(
            execute(
                "private-app",
                """
import http.client, json
from io import BytesIO
from http.cookies import SimpleCookie
from pathlib import Path
"""
                + inspect.getsource(Browser)
                + inspect.getsource(post)
                + inspect.getsource(acknowledge)
                + """
def application(e, start_response):
    c = http.client.HTTPConnection('127.0.0.1',8000,timeout=10)
    headers = {k[5:].replace('_','-'):v for k,v in e.items() if k.startswith('HTTP_')}
    if 'CONTENT_TYPE' in e: headers['Content-Type'] = e['CONTENT_TYPE']
    c.request(e['REQUEST_METHOD'],e['PATH_INFO'],e['wsgi.input'].read(),headers)
    r = c.getresponse()
    start_response(str(r.status),r.getheaders())
    data = r.read()
    c.close()
    return [data]
browser = Browser(application)
p = Path('/var/lib/pilot-app/test-cookies.json')
if p.exists(): browser.cookies = json.loads(p.read_text())
"""
                + action
                + """
p.write_text(json.dumps(browser.cookies))
""",
            )
        )

    # Browser.login checks the conventional WSGI status phrase; adapt the proxy.
    # It is enough to use the same request sequence and cookie handling here.
    eventually(
        lambda: chat("print(json.dumps(browser.request('GET','/login')[0]))") == "200"
    )
    assert (
        chat(
            "browser.request('GET','/login'); print(json.dumps(browser.request('POST','/login',body=('username=alice&password=synthetic-password-alice&csrf='+browser.cookies['ar_login_csrf']).encode(),content_type='application/x-www-form-urlencoded')[0]))"
        )
        == "303"
    )

    def submit(key):
        return chat(
            f"print(json.dumps(json.loads(browser.submit({key!r},'Приватный CANARY-B05D')[2])))"
        )

    eventually(
        lambda: execute(
            "private-app",
            "from pathlib import Path; print(all(Path(p).is_socket() for p in ('/run/pilot-ipc/worker.sock','/run/pilot-research-ipc/research.sock')))",
        )
        == "True"
    )
    assert chat("print(json.dumps(acknowledge(browser)[0]))") == "200"
    first = submit("first")
    assert first.get("model_state") == "completed", first
    for evidence in (
        "same_operation_model",
        "unavailable",
        "contradicts",
        "insufficient",
        "interpretation",
        "not_evaluated",
    ):
        assert evidence in first["response"]
    assert "В первой выборке есть рост, во второй роста нет." in first["response"]

    def owner_request(path, payload=None, *, cookie="", csrf="", method="POST"):
        return json.loads(
            execute(
                "owner-console",
                f"""
import http.client,json
c=http.client.HTTPConnection('127.0.0.1',{port},timeout=3)
c.request({method!r},{path!r},{json.dumps(payload or {})!r},headers={{
    'Host':'owner.test','Origin':'https://owner.test','Content-Type':'application/json',
    'Cookie':{cookie!r},'X-CSRF-Token':{csrf!r},'X-Forwarded-For':'127.0.0.1'}})
r=c.getresponse()
print(json.dumps([r.status,json.loads(r.read()),r.getheaders()]))
c.close()
""",
            )
        )

    _, challenge, headers = owner_request("/login", method="GET")
    cookie = next(v.split(";")[0] for k, v in headers if k == "Set-Cookie")
    code, session, headers = owner_request(
        "/login",
        {
            "username": "operator",
            "password": "synthetic-owner-password",
            "csrf": challenge["csrf"],
        },
        cookie=cookie,
    )
    assert code == 200
    cookie = next(
        v.split(";")[0]
        for k, v in headers
        if k == "Set-Cookie" and v.startswith("ar_owner_session=")
    )
    csrf = session["csrf"]

    def status():
        return owner_request("/status", cookie=cookie)[1]

    usage = status()["worker"]["provider"]
    assert usage["attempt_count"] == 3  # historical + inference + research
    assert usage["recorded_tokens"]["total_tokens"] == "83"  # 15 + 28 + 40
    assert usage["unresolved_reservations"] == 1  # seeded historic unknown
    journal_before = inspect_seed(
        "import sqlite3; print(sqlite3.connect('/seed/worker/research-authority.sqlite3').execute('SELECT * FROM reservations').fetchall())"
    )
    # Match the documented sequenced startup of processes sharing provider.sqlite3.
    # Parallel restart has produced SIGBUS in EventStore initialization; verify
    # each socket before starting the next process, without a provider operation.
    for service, path in (
        ("provider-worker", "/run/pilot-ipc/worker.sock"),
        ("research-worker", "/run/pilot-research-ipc/research.sock"),
    ):
        run(*command, "restart", service)
        eventually(
            lambda: execute(
                "private-app",
                f"from pathlib import Path; print(Path({path!r}).is_socket())",
            )
            == "True"
        )
    run(*command, "restart", "private-app")
    eventually(
        lambda: chat("print(json.dumps(browser.request('GET','/login')[0]))") == "200"
    )
    run(*command, "restart", "owner-console", "worker-telemetry", "worker-stop")
    assert submit("first")["response"] == first["response"]
    eventually(lambda: status()["worker"]["available"])
    assert status()["worker"]["provider"] == usage
    assert (
        inspect_seed(
            "import sqlite3; print(sqlite3.connect('/seed/worker/research-authority.sqlite3').execute('SELECT * FROM reservations').fetchall())"
        )
        == journal_before
    )
    assert (
        len(
            chat(
                "print(json.dumps(json.loads(browser.request('GET','/api/history')[2])['messages']))"
            )
        )
        == 2
    )
    lost = submit("lost")
    assert lost["model_state"] == "research_uncertain"
    # Crash left a socket. Remove only this known fixture's stale socket after
    # its container has exited; ordinary startup itself never unlinks it.
    eventually(lambda: run(*command, "ps", "-a", "--format", "json", "research-worker"))
    inspect_seed(
        "from pathlib import Path; Path('/seed/research-ipc/research.sock').unlink()"
    )
    run(
        *command,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "research-worker",
    )
    eventually(
        lambda: execute(
            "private-app",
            "from pathlib import Path; print(Path('/run/pilot-research-ipc/research.sock').exists())",
        )
        == "True"
    )
    assert submit("lost")["model_state"] == "research_uncertain"
    assert submit("exhausted")["model_state"] == "research_uncertain"
    logs = json.loads(
        inspect_seed(
            "import json; from pathlib import Path; print(json.dumps([json.loads(x) for x in Path('/seed/worker/research-calls.jsonl').read_text().splitlines()]))"
        )
    )
    assert len(logs) == 2
    assert all(
        QUESTIONS[0] in call["input"]
        and "CANARY-B05D" not in str(call)
        and "participant-alice" not in str(call)
        for call in logs
    )
    assert all(call["tools"][0]["type"] == "web_search" for call in logs)
    current = status()["worker"]["provider"]
    assert current["attempt_count"] == 5  # 3 completed above + 2 later inferences
    assert current["unresolved_reservations"] == 2  # historic + lost research

    def emergency():
        code, operation, _ = owner_request(
            "/worker-stop-operation", cookie=cookie, csrf=csrf
        )
        assert code == 200
        return owner_request(
            "/emergency-stop",
            {"operation_id": operation["operation_id"]},
            cookie=cookie,
            csrf=csrf,
        )[:2]

    assert emergency() == [200, {"application": "confirmed", "worker": "confirmed"}]
    assert status()["worker"]["stop"] == {"valid": True, "active": True}
    assert status()["application_stop"]["active"] is True
    # Bypass the application Stop deliberately with an authenticated local
    # worker request: the shared worker Stop independently blocks research.
    probe = """
import json
from pathlib import Path
from app.core.public_research_client import PublicResearchClient
from app.core.public_research_profile import load_research_profile
profile=load_research_profile(Path('/run/pilot-research-application/profile.json'),role='application')
print(json.dumps(PublicResearchClient('/run/pilot-research-ipc/research.sock',expected_worker_uid=11021).execute(profile.request('stop-probe','ocean'))['outcome']))
"""
    assert json.loads(execute("private-app", probe)) == "emergency_stop"
    run(
        *command,
        "restart",
        "research-worker",
        "provider-worker",
        "worker-stop",
        "worker-telemetry",
        "owner-console",
    )
    eventually(lambda: status()["worker"]["available"])
    assert json.loads(execute("private-app", probe)) == "emergency_stop"
    assert status()["worker"]["provider"] == current
    assert submit("stopped")["model_state"] == "stopped"
    run(*command, "stop", "worker-stop", "worker-telemetry")
    assert status()["worker"]["available"] is False
    assert emergency() == [200, {"application": "confirmed", "worker": "unavailable"}]
    # Removing and then restoring a worker projection cannot revive journaled
    # authority; the app's private binding never leaves its mount.
    inspect_seed(
        "from pathlib import Path; p=Path('/seed/research-worker/profile.json'); p.rename(p.with_suffix('.revoked'))"
    )
    assert json.loads(execute("private-app", probe)) == "authorization_denied"
    inspect_seed(
        "from pathlib import Path; p=Path('/seed/research-worker/profile.revoked'); p.rename(p.with_suffix('.json'))"
    )
    run(*command, "stop", "research-worker")
    run(
        *command,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "research-worker",
    )
    eventually(
        lambda: "replayed or changed profile authority"
        in run(*command, "logs", "research-worker")
    )
    # Disabling research uses the original stack and preserves all durable state.
    assert Path(root / "app-command.json").exists()
    assert (
        inspect_seed(
            "from pathlib import Path; print(len(Path('/seed/worker/research-calls.jsonl').read_text().splitlines()))"
        )
        == "2"
    )

    run(*command, "stop", "research-worker", "private-app")
    run(
        *original_pilot,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "private-app",
    )
    eventually(
        lambda: chat("print(json.dumps(browser.request('GET','/login')[0]))") == "200"
    )
    disabled = json.loads(run(*original_pilot, "config", "--format", "json"))[
        "services"
    ]["private-app"]
    assert "--enable-public-research" not in disabled["command"]
    assert not any("research" in m["target"] for m in disabled["volumes"])
    assert submit("first")["model_state"] == "research_denied"
    assert submit("disabled-stopped")["model_state"] == "stopped"
    assert (
        inspect_seed(
            "import sqlite3; print(sqlite3.connect('/seed/worker/research-authority.sqlite3').execute('SELECT COUNT(*) FROM reservations').fetchone()[0])"
        )
        == "2"
    )
