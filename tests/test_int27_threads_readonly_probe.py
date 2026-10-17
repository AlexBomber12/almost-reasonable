"""Synthetic-only evidence for the separate operator read probe."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import runpy
import sqlite3
import sys

import pytest

from app.ops import int27_threads_readonly_probe as probe
from app.threads.transport import HTTPResponse


NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
REV = "a" * 40


class FakeHTTP:
    def __init__(self, responses, after=None):
        self.responses = list(responses)
        self.calls = []
        self.after = after

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, body, timeout))
        if self.after:
            self.after(len(self.calls))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(value, status=200):
    return HTTPResponse(status, json.dumps(value).encode())


@pytest.fixture
def setup(tmp_path):
    directory = tmp_path / "protected"
    directory.mkdir(mode=0o700)
    owner = directory / "owner"
    owner.mkdir(mode=0o700)
    db_path = owner / "owner.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE owner_schema(version INTEGER NOT NULL)")
        db.execute("INSERT INTO owner_schema VALUES(1)")
        db.execute(
            "CREATE TABLE owner_stop(singleton INTEGER PRIMARY KEY, active INTEGER)"
        )
        db.execute("INSERT INTO owner_stop VALUES(1,0)")
    db_path.chmod(0o600)
    token_path = directory / "token"
    token_path.write_text("synthetic-token")
    token_path.chmod(0o400)
    config_path = directory / "config"
    config = {
        "version": probe.VERSION,
        "purpose": "threads-read-contract",
        "reviewed": True,
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "revision": REV,
        "account_ref": "account",
        "provider_user_id": "provider",
        "reply_thread_id": "thread",
        "streams": ["reply", "mention"],
        "owner_directory": str(owner),
        "token_file": str(token_path),
    }

    def save():
        if config_path.exists():
            config_path.chmod(0o600)
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o400)

    save()
    return config, save, config_path, owner, token_path


def run(setup, responses, *, after=None, monotonic=lambda: 0, wall_clock=None):
    client = FakeHTTP(responses, after)
    receipt = probe.run(
        setup[2],
        revision=REV,
        client=client,
        clock=wall_clock or (lambda: NOW),
        monotonic=monotonic,
    )
    return receipt, client


def test_default_has_zero_contacts(monkeypatch, capsys):
    monkeypatch.setattr(probe, "run", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(probe, "HTTPSClient", lambda: pytest.fail("client"))
    assert probe.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "disabled", "requests": 0}


def test_complete_observation_is_bounded_and_sanitized(setup):
    reply = {
        "id": "r1",
        "text": "PRIVATE INBOUND TEXT",
        "timestamp": NOW.isoformat(),
        "media_type": "TEXT_POST",
        "root_post": {"id": "thread"},
        "replied_to": {"id": "thread"},
        "is_reply_owned_by_me": False,
    }
    mention = {
        "id": "m1",
        "text": "OTHER PRIVATE TEXT",
        "timestamp": NOW.isoformat(),
        "media_type": "TEXT_POST",
    }
    receipt, client = run(
        setup,
        [
            response({"id": "provider"}),
            response({"data": [reply]}),
            response({"data": [mention]}),
        ],
    )
    assert receipt["outcome"] == "observed"
    assert receipt["requests"] == 3
    assert [row["rows"] for row in receipt["observed"][1:]] == [1, 1]
    assert "PRIVATE" not in json.dumps(receipt)
    assert "synthetic-token" not in json.dumps(receipt)
    assert all(call[0] == "GET" and call[2] is None for call in client.calls)
    assert all("limit=10" in call[1] for call in client.calls[1:])
    assert all(call[3] <= 10 for call in client.calls)


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(version="wrong"),
        lambda c: c.update(reviewed=False),
        lambda c: c.update(purpose="publish"),
        lambda c: c.update(expires_at=(NOW - timedelta(seconds=1)).isoformat()),
        lambda c: c.update(expires_at=(NOW + timedelta(hours=2)).isoformat()),
        lambda c: c.update(revision="other"),
        lambda c: c.update(streams="reply"),
        lambda c: c.update(streams=[]),
        lambda c: c.update(streams=["reply", "reply"]),
        lambda c: c.update(streams=["arbitrary"]),
        lambda c: c.update(provider_user_id="https://evil.example"),
        lambda c: c.update(account_ref="bad account"),
        lambda c: c.update(reply_thread_id="bad/thread"),
        lambda c: c.update(token_file="relative"),
        lambda c: c.update(owner_directory="relative"),
        lambda c: c.update(expires_at="2026-10-17T12:15:00"),
        lambda c: c.update(extra="unexpected"),
    ],
)
def test_config_rejects_unreviewed_unbound_or_expired(setup, change):
    config, save, *_ = setup
    change(config)
    save()
    receipt, client = run(setup, [])
    assert receipt["outcome"] == "configuration_invalid"
    assert receipt["requests"] == 0
    assert client.calls == []


def test_protected_file_checks_mode_and_ancestry(setup, tmp_path):
    _, _, config_path, _, token = setup
    config_path.chmod(0o600)
    assert run(setup, [])[0]["outcome"] == "configuration_invalid"
    config_path.chmod(0o400)
    token.chmod(0o600)
    assert run(setup, [])[0]["outcome"] == "configuration_invalid"
    token.chmod(0o400)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    assert (
        probe.run(
            unsafe / "missing", revision=REV, client=FakeHTTP([]), clock=lambda: NOW
        )["outcome"]
        == "configuration_invalid"
    )
    with pytest.raises(probe.ProbeRejected):
        probe._protected_file(Path("relative"), 10)
    token.parent.chmod(0o755)
    assert run(setup, [])[0]["outcome"] == "configuration_invalid"


@pytest.mark.parametrize(
    "first,expected",
    [
        (response({"id": "wrong"}), "account_mismatch"),
        (response({"error": {"message": "secret"}}, 401), "authentication_failure"),
        (response({"error": {"message": "secret"}}, 429), "rate_limited"),
        (response({"error": {"message": "secret"}}, 500), "provider_failure"),
        (response({}, 302), "redirect_rejected"),
        (HTTPResponse(200, b"{bad"), "contract_malformed"),
        (HTTPResponse(200, b"x" * 2_097_153), "response_oversized"),
        (HTTPResponse(200, b"[]"), "contract_malformed"),
        (response({"id": 1}), "contract_malformed"),
        (HTTPResponse(200, "not bytes"), "response_oversized"),
        (object(), "response_oversized"),
        (TimeoutError("token-bearing URL"), "transport_failure"),
    ],
)
def test_account_failure_stops_all_following_reads(setup, first, expected):
    receipt, client = run(setup, [first])
    assert receipt["outcome"] == expected
    assert receipt["requests"] == 1
    assert len(client.calls) == 1
    assert "secret" not in json.dumps(receipt)
    assert "token-bearing" not in json.dumps(receipt)


def test_stop_change_blocks_second_request(setup):
    owner = setup[3]

    def after(number):
        if number == 1:
            with sqlite3.connect(owner / "owner.sqlite") as db:
                db.execute("UPDATE owner_stop SET active=1")

    receipt, client = run(setup, [response({"id": "provider"})], after=after)
    assert receipt["outcome"] == "owner_stop"
    assert receipt["requests"] == 1
    assert len(client.calls) == 1


def test_expiry_before_and_between_requests_blocks_dispatch(setup):
    times = iter([NOW, NOW + timedelta(minutes=16)])
    receipt, client = run(
        setup,
        [response({"id": "provider"})],
        wall_clock=lambda: next(times),
    )
    assert receipt["outcome"] == "configuration_expired"
    assert client.calls == []
    times = iter([NOW, NOW, NOW + timedelta(minutes=16)])
    receipt, client = run(
        setup, [response({"id": "provider"})], wall_clock=lambda: next(times)
    )
    assert receipt["outcome"] == "configuration_expired"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("POST", "https://graph.threads.net/v1.0/me?fields=id", b""),
        ("DELETE", "https://graph.threads.net/v1.0/me?fields=id", None),
        ("GET", "https://evil.example/v1.0/me?fields=id", None),
        ("GET", "https://graph.threads.net/v1.0/me?fields=id&after=next", None),
    ],
)
def test_boundary_rejects_method_host_body_and_caller_urls(setup, method, url, body):
    boundary = probe.ReadBoundary(FakeHTTP([]), setup[0], 30, lambda: 0, lambda: NOW)
    with pytest.raises(probe.ProbeRejected, match="request_rejected"):
        boundary.request(method, url, {}, body, 5)
    assert boundary.requests == 0


def test_deadline_prevents_dispatch(setup):
    boundary = probe.ReadBoundary(FakeHTTP([]), setup[0], 0, lambda: 0, lambda: NOW)
    with pytest.raises(probe.ProbeRejected, match="deadline_exceeded"):
        boundary.request(
            "GET", "https://graph.threads.net/v1.0/me?fields=id", {}, None, 5
        )
    assert boundary.requests == 0


def test_deadline_after_response_blocks_following_reads(setup):
    ticks = iter([0, 0, 31])
    receipt, client = run(
        setup, [response({"id": "provider"})], monotonic=lambda: next(ticks)
    )
    assert receipt["outcome"] == "deadline_exceeded"
    assert len(client.calls) == 1


def test_deadline_between_requests_and_bad_token(setup):
    ticks = iter([0, 0, 0, 31])
    receipt, client = run(
        setup, [response({"id": "provider"})], monotonic=lambda: next(ticks)
    )
    assert receipt["outcome"] == "deadline_exceeded"
    assert len(client.calls) == 1
    token = setup[4]
    token.chmod(0o600)
    token.write_text("  ")
    token.chmod(0o400)
    receipt, client = run(setup, [])
    assert receipt["outcome"] == "configuration_invalid"
    assert client.calls == []


def test_no_owner_store_creation(setup):
    (setup[3] / "owner.sqlite").unlink()
    receipt, client = run(setup, [response({"id": "provider"})])
    assert receipt["outcome"] == "owner_stop"
    assert client.calls == []
    assert not (setup[3] / "owner.sqlite").exists()


def test_owner_store_unsafe_file_and_schema_fail_closed(setup):
    db_path = setup[3] / "owner.sqlite"
    db_path.chmod(0o644)
    assert run(setup, [response({"id": "provider"})])[0]["outcome"] == "owner_stop"
    db_path.chmod(0o600)
    safe_sidecar = Path(str(db_path) + "-journal")
    safe_sidecar.touch(mode=0o600)
    assert probe._owner_stopped(setup[3]) is False
    safe_sidecar.unlink()
    sidecar = Path(str(db_path) + "-wal")
    sidecar.write_text("unsafe")
    sidecar.chmod(0o644)
    assert run(setup, [response({"id": "provider"})])[0]["outcome"] == "owner_stop"
    sidecar.unlink()
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE owner_schema SET version=2")
    assert run(setup, [response({"id": "provider"})])[0]["outcome"] == "owner_stop"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE owner_schema SET version=1")
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE owner_stop")
    assert run(setup, [response({"id": "provider"})])[0]["outcome"] == "owner_stop"


def test_single_streams_and_page_contract_rejection(setup):
    config, save, *_ = setup
    for stream in ("reply", "mention"):
        config["streams"] = [stream]
        save()
        receipt, client = run(
            setup, [response({"id": "provider"}), response({"data": []})]
        )
        assert receipt["outcome"] == "observed" and len(client.calls) == 2
    for invalid in (
        {"data": [1]},
        {"data": [{}] * 11},
        {"data": "invalid"},
        {"data": [{}]},
    ):
        receipt, client = run(setup, [response({"id": "provider"}), response(invalid)])
        assert receipt["outcome"] == "contract_malformed" and len(client.calls) == 2


def test_reviewed_reverse_stream_order_is_observed(setup):
    config, save, *_ = setup
    config["streams"] = ["mention", "reply"]
    save()
    receipt, client = run(
        setup,
        [response({"id": "provider"}), response({"data": []}), response({"data": []})],
    )
    assert receipt["outcome"] == "observed"
    assert [row["operation"] for row in receipt["observed"]] == [
        "account",
        "mention",
        "reply",
    ]
    assert "/mentions?" in client.calls[1][1]
    assert "/replies?" in client.calls[2][1]


def test_boundaries_reject_fourth_request_and_response_link(setup):
    client = FakeHTTP([response({"id": "provider", "next": "https://evil.example"})])
    boundary = probe.ReadBoundary(client, setup[0], 30, lambda: 0, lambda: NOW)
    boundary.request("GET", "https://graph.threads.net/v1.0/me?fields=id", {}, None, 5)
    assert len(client.calls) == 1
    boundary.requests = 3
    with pytest.raises(probe.ProbeRejected, match="request_rejected"):
        boundary.request(
            "GET", "https://graph.threads.net/v1.0/me?fields=id", {}, None, 5
        )
    assert len(client.calls) == 1


def test_main_run_and_module_entrypoint_are_explicit(setup, monkeypatch, capsys):
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: REV)
    monkeypatch.setattr(probe, "run", lambda *a, **k: {"outcome": "observed"})
    assert probe.main(["run", "--config", str(setup[2])]) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "observed"}
    monkeypatch.setattr(probe, "run", lambda *a, **k: {"outcome": "owner_stop"})
    assert probe.main(["run", "--config", str(setup[2])]) == 1
    with pytest.raises(SystemExit):
        probe.main(["run"])
    monkeypatch.setattr(sys, "argv", ["int27_threads_readonly_probe"])
    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        with pytest.raises(SystemExit) as result:
            runpy.run_module(
                "app.ops.int27_threads_readonly_probe", run_name="__main__"
            )
    assert result.value.code == 0
