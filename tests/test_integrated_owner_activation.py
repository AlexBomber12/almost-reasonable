"""Native public worker Stop and sanitized owner observation boundaries."""

import json
import os
import pytest


@pytest.mark.parametrize("when", ["before", "admitted", "during"])
def test_public_model_worker_consumes_its_native_stop_file(tmp_path, when):
    from app.trusted.worker_stop import WorkerStop, assert_stop
    from app.trusted.public_behavior_worker import PublicWorkerServer
    from test_int24_runtime import setup, run_once

    folder = tmp_path / "public"
    folder.mkdir(mode=0o700)
    app, original, sdk, http, _ = setup(
        folder, {"decision": "no_action", "reason_category": "other"}
    )
    original.close()
    control = folder / "stop.json"
    control.write_text(json.dumps({"version": "public-model-stop-v1", "stop": False}))
    control.chmod(0o400)
    stop = WorkerStop(control, "public-model-stop-v1")
    worker = PublicWorkerServer(
        original.socket_path,
        original.attempts_path,
        permitted_uid=os.geteuid(),
        attest=app.attest_worker_request,
        adapter=original.adapter,
        provider_store=app.provider,
        emergency_stop=stop.active,
    )
    worker.start()
    if when == "before":
        assert_stop(control, stop_version="public-model-stop-v1")
    elif when == "admitted":
        reserve = app.provider.reserve_attempt

        def reserve_then_stop(*args, **kwargs):
            result = reserve(*args, **kwargs)
            assert_stop(control, stop_version="public-model-stop-v1")
            return result

        app.provider.reserve_attempt = reserve_then_stop
    else:
        create = sdk.create

        def stop_after_sdk(**kwargs):
            result = create(**kwargs)
            assert_stop(control, stop_version="public-model-stop-v1")
            return result

        sdk.create = stop_after_sdk
    try:
        run_once(app, worker)
        assert len(sdk.calls) == (when == "during")
        assert stop.active() and http.calls == []
        with app.journal.transaction() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM agent_intellectual_revisions"
                ).fetchone()[0]
                == 0
            )
    finally:
        worker.close()


def test_public_worker_requires_explicit_stop_before_loading_provider_credentials(
    monkeypatch,
):
    from app.ops import int24_public_worker as module

    monkeypatch.setattr(
        module,
        "_private_config",
        lambda _: {
            "worker_enabled": True,
            "project_id": "proj_test",
            "organization_id": "org-test",
        },
    )
    monkeypatch.setattr(
        module,
        "read_protected_worker_file",
        lambda *_args, **_kwargs: pytest.fail(
            "credential read before Stop configuration"
        ),
    )
    with pytest.raises(ValueError, match="Stop"):
        module.main(["--config", "/protected/config"])
