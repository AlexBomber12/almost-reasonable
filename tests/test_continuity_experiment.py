"""Pre-registered journey regressions; synthetic transports are not human acceptance."""

import json
import sqlite3

import pytest

from app.continuity_experiment import Lab, LabError, ModelFailure


@pytest.fixture
def lab(tmp_path):
    return Lab.create(tmp_path / "experiment", participants=("a", "b"))


def decision(action="reply", **changes):
    return (
        dict(
            action=action,
            text="Я пока сохраняю сомнение.",
            reason="Возражение требует проверки.",
            based_on=["post"],
            question_key=None,
            updates=[],
        )
        | changes
    )


class Model:
    def __init__(self, result=None, failure=None):
        self.result = decision() if result is None else result
        self.failure = failure
        self.frames = []

    def complete(self, frame):
        self.frames.append(frame)
        if self.failure is not None:
            raise self.failure
        return self.result


def post(lab):
    lab.add("post", "public", "post", "Мой исходный тезис.", source="operator:test")


def update(**changes):
    return (
        dict(
            key="uncertainty",
            kind="position",
            text="Нужно различать отсутствие связи и отсутствие данных.",
            reason="Это существенное ограничение вывода.",
            sources=["post"],
            base_revision=0,
        )
        | changes
    )


def test_e01_e04_significant_experience_survives_noise_restart_and_revision(lab):
    post(lab)
    result = lab.step("first", "post", Model(decision(updates=[update()])))
    assert result["status"] == "decided"
    assert result["effect"] == "shadow_only"
    for i in range(100):
        lab.add(f"noise-{i}", "public", "wake", "Проверка активности.")
    reopened = Lab(lab.root)
    frame = reopened.context("noise-99")
    assert frame["experience"]["posts"][0]["id"] == "post"
    assert frame["memory"][0]["revision"] == 1
    assert frame["memory"][0]["sources"] == ["post"]
    assert any(e["id"] == "post" for e in frame["sources"])
    assert frame["omitted_events"] > 90
    assert len(frame["recent"]) <= 4
    revised = Model(
        decision(
            based_on=["post", "noise-99"],
            updates=[update(base_revision=1, text="Уточнённая позиция.")],
        )
    )
    assert reopened.step("second", "noise-99", revised)["status"] == "decided"
    assert reopened.inspect("public")["memory"][0]["revision"] == 2
    assert reopened.context("noise-99")["memory"][0]["previous"]["revision"] == 1
    with sqlite3.connect(lab.root / "lab.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 2


def test_e02_e03_comment_question_research_and_later_private_discussion(lab):
    post(lab)
    lab.add("comment", "public", "comment", "Но выборка мала.", source="operator:test")
    model = Model(
        decision(
            "research",
            text="Какая выборка позволяет заметить эффект?",
            based_on=["comment"],
            question_key="sample",
            updates=[update(key="sample", kind="question", sources=["comment"])],
        )
    )
    assert lab.step("inquiry", "comment", model)["status"] == "decided"
    assert model.frames[0]["experience"]["comments"][0]["id"] == "comment"
    blocked = lab.step("repeat", "comment", Model(model.result | {"updates": []}))
    assert blocked["error"] == "research_pending"
    lab.add(
        "evidence",
        "public",
        "research",
        "Результат с ограничениями.",
        source="operator:https://example.test/paper",
        parent="inquiry",
    )
    lab.add("ask", "a", "message", "Что изменилось после возражения?")
    frame = lab.context("ask")
    assert frame["experience"]["research"][0]["parent"] == "inquiry"
    assert frame["memory"][0]["kind"] == "question"
    assert (
        lab.step("discussion", "ask", Model(decision(based_on=["evidence"])))["status"]
        == "decided"
    )


def test_e05_private_memories_and_inputs_never_cross_scopes(lab):
    post(lab)
    lab.add("private-a", "a", "message", "synthetic-a-canary")
    saved = lab.step(
        "remember-a",
        "private-a",
        Model(
            decision(
                based_on=["private-a"],
                updates=[update(sources=["private-a"])],
            )
        ),
    )
    assert saved["status"] == "decided"
    lab.add("private-b", "b", "message", "synthetic-b-canary")
    for event in ["private-b", "post"]:
        frame = json.dumps(lab.context(event))
        assert "synthetic-a-canary" not in frame
        assert "remember-a" not in frame
    assert lab.context("private-a")["memory"][0]["scope"] == "a"
    assert lab.inspect("b")["memory"] == []
    assert lab.step("remember-a", "private-b", Model())["error"] == "operation_conflict"


def test_e07_failure_is_terminal_distinct_from_deliberate_silence(lab):
    post(lab)
    silent = Model(decision("no_action", text=""))
    good = lab.step("quiet", "post", silent)
    assert good["status"] == "decided"
    assert good["decision"]["action"] == "no_action"
    for code in [
        "incomplete",
        "timeout",
        "provider_error",
        "budget_blocked",
        "stopped",
    ]:
        model = Model(failure=ModelFailure(code))
        result = lab.step(code, "post", model)
        assert result["status"] == "failed"
        assert result["error"] == code
        assert "decision" not in result
        assert lab.step(code, "post", model) == result
        assert len(model.frames) == 1
    assert lab.inspect("public")["memory"] == []


def test_e08_crash_recovery_and_no_implicit_replay(lab):
    post(lab)
    with pytest.raises(KeyboardInterrupt):
        lab.step("crashed", "post", Model(failure=KeyboardInterrupt()))
    reopened = Lab(lab.root)
    assert reopened.recover() == 1
    assert reopened.recover() == 0
    model = Model()
    result = reopened.step("crashed", "post", model)
    assert result["status"] == "unknown"
    assert model.frames == []
    assert reopened.step("new-operation", "post", model)["status"] == "decided"
    assert len(model.frames) == 1


def test_duplicate_event_is_idempotent_but_changed_input_is_rejected(lab):
    post(lab)
    post(lab)
    with pytest.raises(LabError, match="event_conflict"):
        lab.add("post", "public", "post", "Изменённый текст.", source="operator:test")
    assert lab.inspect("public")["event_count"] == 1


def test_existing_directory_and_unmarked_store_are_never_adopted(tmp_path):
    root = tmp_path / "production"
    root.mkdir()
    history = root / "lab.sqlite"
    history.write_bytes(b"existing history")
    with pytest.raises(FileExistsError):
        Lab.create(root)
    before = history.read_bytes()
    with pytest.raises(LabError):
        Lab(root)
    assert history.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"action": "publish"},
        {"based_on": ["missing"]},
        {"updates": [update(sources=["missing"])]},
        {"updates": [update(base_revision=8)]},
        {"extra": "not allowed"},
        {"text": "x" * 5000},
        {"action": "research", "question_key": None},
    ],
)
def test_invalid_decisions_have_no_memory_or_external_effect(lab, change):
    post(lab)
    result = lab.step("invalid", "post", Model(decision(**change)))
    assert result["status"] == "failed"
    assert lab.inspect("public")["memory"] == []


def test_inference_is_not_called_if_required_context_would_be_dropped(lab, monkeypatch):
    post(lab)
    import app.continuity_experiment as module

    monkeypatch.setattr(module, "MAX_FRAME_BYTES", 64)
    model = Model()
    result = lab.step("too-large", "post", model)
    assert result["error"] == "context_full"
    assert model.frames == []


def test_storage_lock_blocks_concurrent_admission_and_recovery(lab):
    post(lab)
    with sqlite3.connect(lab.root / "mutex.sqlite") as mutex:
        mutex.execute("BEGIN IMMEDIATE")
        with pytest.raises(LabError, match="busy"):
            lab.step("blocked", "post", Model())
        with pytest.raises(LabError, match="busy"):
            lab.recover()


def test_scope_and_source_validation(lab):
    for args in [
        ("bad", "stranger", "message", "Текст"),
        ("bad", "a", "post", "Текст"),
        ("bad", "public", "post", "Текст"),
        ("bad", "public", "research", "Текст"),
        ("bad", "public", "wake", ""),
    ]:
        with pytest.raises(LabError):
            lab.add(*args)
    with pytest.raises(LabError, match="event_missing"):
        lab.context("missing")


def test_manifest_and_storage_identity_are_checked_before_reads(lab):
    original = lab.root / "lab.sqlite"
    moved = lab.root / "old.sqlite"
    original.rename(moved)
    original.write_bytes(moved.read_bytes())
    with pytest.raises(LabError, match="storage_changed"):
        lab.inspect("public")
    assert moved.is_file()


def test_unknown_model_exception_is_sanitized(lab):
    post(lab)
    result = lab.step(
        "exception", "post", Model(failure=RuntimeError("private provider detail"))
    )
    assert result["error"] == "provider_error"
    assert "private provider detail" not in json.dumps(result)


def test_state_bound_is_explicit_instead_of_silently_evicting(lab, monkeypatch):
    post(lab)
    import app.continuity_experiment as module

    monkeypatch.setattr(module, "MAX_STATES", 1)
    bad = Model(decision(updates=[update(), update(key="second")]))
    assert lab.step("full", "post", bad)["error"] == "memory_full"
    assert lab.inspect("public")["memory"] == []


@pytest.mark.parametrize(
    "participants", [("a",), ("a", "a"), ("public", "b"), ("bad/name", "b")]
)
def test_invalid_participants_cannot_create_storage(tmp_path, participants):
    root = tmp_path / "invalid"
    with pytest.raises(LabError):
        Lab.create(root, participants=participants)
    assert not root.exists()


def test_research_result_requires_existing_research_intent(lab):
    post(lab)
    lab.step("reply", "post", Model())
    for parent in ["missing", "reply"]:
        with pytest.raises(LabError, match="research_parent_missing"):
            lab.add(
                "result",
                "public",
                "research",
                "Не подтверждено.",
                source="operator:test",
                parent=parent,
            )
    with pytest.raises(LabError, match="unexpected_parent"):
        lab.add("message", "a", "message", "Текст", parent="reply")


def test_unmarked_wrong_version_and_corrupt_database_are_rejected(tmp_path):
    for label in ["unmarked", "version", "corrupt"]:
        obj = Lab.create(tmp_path / label)
        with sqlite3.connect(obj.path) as db:
            if label == "unmarked":
                db.execute("PRAGMA application_id=0")
            elif label == "version":
                db.execute("UPDATE metadata SET version='wrong'")
            else:
                db.execute("DROP TABLE metadata")
        with pytest.raises(LabError, match="not_experiment"):
            Lab(obj.root)


def test_linked_or_replaced_storage_is_never_followed(tmp_path):
    target = Lab.create(tmp_path / "target")
    link = tmp_path / "alias"
    link.symlink_to(target.root, target_is_directory=True)
    with pytest.raises(LabError, match="unsafe_directory"):
        Lab(link)
    with pytest.raises(LabError, match="unsafe_directory"):
        Lab.create(link / "child")
    original = target.root / "mutex.sqlite"
    original.unlink()
    original.symlink_to(target.path)
    with pytest.raises(LabError, match="unsafe_storage"):
        Lab(target.root)


@pytest.mark.parametrize(
    "change",
    [
        {"updates": "bad"},
        {"updates": [update()] * 5},
        {"updates": [{}]},
        {"updates": [update(), update()]},
        {"updates": [update(kind="biography")]},
        {"based_on": []},
        {"based_on": [1]},
        {"based_on": "post"},
        {"question_key": "unexpected"},
        {"updates": [update(key="invalid/key")]},
    ],
)
def test_malformed_model_changes_are_atomic(lab, change):
    post(lab)
    assert (
        lab.step("malformed", "post", Model(decision(**change)))["status"] == "failed"
    )
    assert lab.inspect("public")["memory"] == []


def test_context_full_state_limit_and_missing_step_do_not_call_model(lab, monkeypatch):
    import app.continuity_experiment as module

    post(lab)
    lab.step("remember", "post", Model(decision(updates=[update()])))
    monkeypatch.setattr(module, "MAX_STATES", 0)
    assert lab.step("bounded", "post", Model())["error"] == "memory_full"
    with pytest.raises(LabError, match="event_missing"):
        lab.step("missing", "missing", Model())


def test_research_can_continue_after_material_arrives_but_only_publicly(lab):
    post(lab)
    lab.add("private", "a", "message", "Частный вопрос")
    private = decision(
        "research",
        based_on=["private"],
        question_key="q",
        updates=[update(key="q", kind="question", sources=["private"])],
    )
    assert (
        lab.step("private-inquiry", "private", Model(private))["error"]
        == "invalid_research_question"
    )
    wrong_kind = decision("research", question_key="q", updates=[update(key="q")])
    assert (
        lab.step("wrong-kind", "post", Model(wrong_kind))["error"]
        == "invalid_research_question"
    )
    query = decision(
        "research", question_key="q", updates=[update(key="q", kind="question")]
    )
    lab.step("ordinary", "post", Model())
    assert lab.step("query", "post", Model(query))["status"] == "decided"
    lab.add(
        "answer",
        "public",
        "research",
        "Материал для продолжения",
        source="operator:test",
        parent="query",
    )
    assert (
        lab.step("follow-up", "answer", Model(query | {"updates": []}))["status"]
        == "decided"
    )


def test_failed_provider_timing_is_nonnegative(lab):
    post(lab)
    result = lab.step("failure", "post", Model(failure=ModelFailure("timeout")))
    assert all(ms >= 0 for ms in result["timing_ms"].values())


def test_unrecallable_update_is_rolled_back_with_terminal_failure(lab, monkeypatch):
    import app.continuity_experiment as module

    post(lab)
    original = len(module._json(lab.context("post")).encode())
    monkeypatch.setattr(module, "MAX_FRAME_BYTES", original + 10)
    model = Model(decision(updates=[update()]))
    result = lab.step("too-much-memory", "post", model)
    assert result["error"] == "context_full"
    assert len(model.frames) == 1
    assert lab.inspect("public")["memory"] == []
    with sqlite3.connect(lab.path) as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
