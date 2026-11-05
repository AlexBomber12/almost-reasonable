"""Public observation reports lifecycle facts without private prose or verdicts."""

import json

import test_owner_public_runtime as runtime_cases
from test_owner_public_runtime import view

pending_public = runtime_cases.pending_public


def test_owner_distinguishes_null_proposals_from_rejected_and_recorded_changes(
    pending_public,
):
    app = pending_public[0]
    baseline = view(app).owner_status()["development"]
    with app.journal.transaction() as db:
        for index, status in enumerate(
            ("reserved", "received", "no_change", "rejected", "accepted", "unavailable")
        ):
            db.execute(
                "INSERT INTO agent_intellectual_operations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "public",
                    app.account,
                    f"public-{index}",
                    "binding",
                    "controls",
                    None,
                    "null"
                    if status == "no_change"
                    else '{"reason":"PUBLIC_PROSE_CANARY"}',
                    None,
                    status,
                ),
            )
        db.execute(
            "INSERT INTO agent_intellectual_operations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "private",
                "participant-a",
                "PRIVATE_OPERATION_CANARY",
                "b",
                "c",
                None,
                '"PRIVATE_PROSE_CANARY"',
                None,
                "accepted",
            ),
        )
        db.execute(
            "INSERT INTO agent_intellectual_operations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "public",
                "foreign-account",
                "FOREIGN_OPERATION_CANARY",
                "b",
                "c",
                None,
                None,
                None,
                "accepted",
            ),
        )
        for revision, payload in ((1, '"PUBLIC_PROSE_CANARY"'), (2, None)):
            db.execute(
                "INSERT INTO agent_intellectual_revisions VALUES(?,?,?,?,?,?,?)",
                (
                    "public",
                    app.account,
                    "item",
                    revision,
                    f"revision-{revision}",
                    payload,
                    "{}",
                ),
            )
    status = view(app).owner_status()
    development = status["development"]
    assert development["scope"] == "recorded_public_lifecycle"
    assert development["operations"] == {
        key: value + 1 for key, value in baseline["operations"].items()
    }
    assert development["recorded_revisions"] == baseline["recorded_revisions"] + 2
    assert development["redacted_revisions"] == baseline["redacted_revisions"] + 1
    assert len(development["recent"]) == min(8, len(baseline["recent"]) + 6)
    unchanged = next(
        row for row in development["recent"] if row["status"] == "no_change"
    )
    assert unchanged["proposal_recorded"] is False
    assert "CANARY" not in json.dumps(status)
    assert "public-" not in json.dumps(development)
    assert "current_positions" not in development


def test_owner_reports_missing_reply_poll_separately_from_failed_or_empty_poll(
    pending_public,
):
    app = pending_public[0]
    status = view(app).owner_status()["discussions"]
    assert status["streams"]["reply"]["last_attempt"] is None
    assert status["streams"]["reply"]["received"] == 0
    with app.service.store._open() as db:
        for index, state in enumerate(("committed", "failed")):
            db.execute(
                "INSERT INTO threads_poll_attempts VALUES(?,?,?,?,?,?,?)",
                (
                    f"poll-{index}",
                    app.account,
                    "reply",
                    "PRIVATE_ACTOR_CANARY",
                    state,
                    f"2026-11-05T12:00:0{index}+00:00",
                    None,
                ),
            )
        db.execute(
            "INSERT INTO threads_poll_attempts VALUES(?,?,?,?,?,?,?)",
            (
                "foreign-poll",
                "foreign-account",
                "reply",
                "FOREIGN_CANARY",
                "committed",
                "2026-11-05T13:00:00+00:00",
                None,
            ),
        )
    status = view(app).owner_status()
    reply = status["discussions"]["streams"]["reply"]
    assert reply["last_attempt"] == {
        "status": "failed",
        "started_at": "2026-11-05T12:00:01+00:00",
        "completed_at": None,
    }
    assert reply["attempts"] == {"committed": 1, "failed": 1}
    assert reply["received"] == 0
    assert "CANARY" not in json.dumps(status)
    assert "healthy" not in status["discussions"]


def test_owner_separates_allowance_denial_from_failure_and_unresolved_work(
    pending_public,
):
    app = pending_public[0]
    with app.journal.transaction() as db:
        for index, (phase, evidence) in enumerate(
            (
                ("failed", json.dumps({"outcome": "allowance_exhausted"})),
                ("failed", "PRIVATE_REPORT_CANARY"),
                ("held", None),
                ("started", None),
            )
        ):
            db.execute(
                "INSERT INTO public_research_ops VALUES(?,?,?,?,?,?)",
                (
                    f"research-{index}",
                    f"request-{index}",
                    "PRIVATE_TOPIC_CANARY",
                    "profile",
                    phase,
                    evidence,
                ),
            )
    status = view(app).owner_status()
    assert status["research"]["allowance_exhausted_operations"] == 1
    assert status["research"]["failed_operations"] == 2
    assert status["research"]["held_operations"] == 1
    assert status["research"]["inflight_operations"] == 1
    assert status["research"]["completed_reports"] == 0
    assert "CANARY" not in json.dumps(status)
