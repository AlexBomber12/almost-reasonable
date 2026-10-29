"""Private source adapter for agent-owned state; caller holds PrivacyReadGate."""

from contextlib import closing
from dataclasses import replace
import json

from app.core.intellectual_state import IntellectualStateStore, initialize, digest
from app.cognition.shared_context import SharedModelRequest


class PrivateIntellectualState:
    def __init__(self, runtime):
        self.app = runtime
        with closing(runtime._event_store._open()) as db, db:
            initialize(db)

    def state(self, principal):
        return IntellectualStateStore("private", principal.principal_id.value)

    def controls(self):
        return str(self.app._gate._ledger.control_generation())

    def sources(self, principal, db):
        app = self.app
        result = {}
        for event in app._event_store.retrieve_private_events(principal):
            result["event:" + event.event_id] = {
                "kind": "participant_assertion",
                "text": event.content,
                "binding": event.event_id,
                "dependencies": {},
            }
        for (operation,) in db.execute(
            "SELECT operation_id FROM agent_reply_operations WHERE audience_principal_id=? ORDER BY operation_id",
            (principal.principal_id.value,),
        ):
            reply = app._outputs._read(db, principal, operation)
            if (
                reply is not None
                and app._shared_consent_live(reply.operation, principal)
                and app._narrative_reply_permitted(reply.operation)
            ):
                result["context:" + operation] = {
                    "kind": "participant_assertion",
                    "text": "",
                    "binding": operation,
                    "dependencies": {},
                }
                for event in reply.operation.source_event_ids:
                    result.setdefault(
                        "event:" + event,
                        {
                            "kind": "participant_assertion",
                            "text": "",
                            "binding": event,
                            "dependencies": {},
                        },
                    )
                for shared in reply.operation.shared_operation_ids:
                    for (event,) in db.execute(
                        "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                        (shared,),
                    ):
                        result.setdefault(
                            "event:" + event,
                            {
                                "kind": "participant_assertion",
                                "text": "",
                                "binding": event,
                                "dependencies": {},
                            },
                        )
            if (
                reply is None
                or reply.content is None
                or reply.operation.narrative_operation_ids
            ):
                continue
            if db.execute(
                "SELECT outcome,release_id FROM agent_reply_resolutions WHERE operation_id=?",
                (operation,),
            ).fetchone() != ("completed", "release:" + operation):
                continue
            if (
                not app._shared_consent_live(reply.operation, principal)
                or not app._narrative_reply_permitted(reply.operation)
                or not app._historical_research_permitted(principal, reply)
            ):
                continue
            deps = {
                "event:" + event: event for event in reply.operation.source_event_ids
            }
            deps.update(self.state(principal).dependencies(db, operation))
            result["reply:" + operation] = {
                "kind": "server_released_agent_response",
                "text": reply.content,
                "binding": "release:" + operation,
                "dependencies": deps,
            }
        # A later expression cannot launder a revoked/deleted ancestor.
        for key in tuple(result):
            if not IntellectualStateStore._valid_dependencies(
                result[key]["dependencies"], result
            ):
                del result[key]
        return result

    def attach(self, db, principal, operation, request, event_ids):
        state = self.state(principal)
        sources = self.sources(principal, db)
        visible = {"event:" + event for event in event_ids}
        visible.update(
            sorted((key for key in sources if key.startswith("reply:")), reverse=True)[
                :3
            ]
        )
        private = request.private if type(request) is SharedModelRequest else request
        budget = (
            self.app._limits.max_utf8_bytes - len(request.serialize().encode()) - 64
        )
        if budget < 128:
            return request, event_ids
        from app.core.intellectual_state import IntellectualCapacityError

        required = {"event:" + event: event for event in event_ids}
        required["context:" + operation] = operation
        shared = list(private.inherited_shared_operation_ids)
        if type(request) is SharedModelRequest:
            shared.extend(record.operation_id for record in request.shared)
        for shared_operation in shared:
            for (event,) in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (shared_operation,),
            ):
                required["event:" + event] = event
        try:
            value = state.reserve(
                db,
                operation,
                sources,
                digest(json.loads(request.serialize())),
                self.controls(),
                visible_keys=visible,
                budget=budget,
                required_dependencies=required,
                query=private.conversation.conversation[-1].content,
            )
        except IntellectualCapacityError:
            return request, event_ids
        trial = replace(private, intellectual_state=value)
        result = (
            replace(request, private=trial)
            if type(request) is SharedModelRequest
            else trial
        )
        roots = {
            key.removeprefix("event:")
            for key in state.dependencies(db, operation)
            if key.startswith("event:")
        }
        own_roots = {
            event
            for event in roots
            if db.execute(
                "SELECT 1 FROM event_envelopes WHERE event_id=? AND audience_principal_id=?",
                (event, principal.principal_id.value),
            ).fetchone()
            is not None
        }
        return result, tuple(sorted(set(event_ids) | own_roots))

    def inherited_operations(self, db, principal, operation):
        groups = [set(), set(), set()]
        for key in self.state(principal).dependencies(db, operation):
            if key.startswith("context:") and key != "context:" + operation:
                reply = self.app._outputs._read(
                    db, principal, key.removeprefix("context:")
                )
                if reply is None:
                    raise ValueError("intellectual context disappeared")
                for target, values in zip(
                    groups,
                    (
                        reply.operation.shared_operation_ids,
                        reply.operation.generic_operation_ids,
                        reply.operation.narrative_operation_ids,
                    ),
                ):
                    target.update(values)
        return groups

    def bind_context(self, db, principal, operation, request):
        """Protect the complete admitted context, including shared/narrative graphs."""
        state = self.state(principal)
        row = state._operation(db, operation)
        if row is None:
            return
        snapshot = json.loads(row[2])
        private = request.private if type(request) is SharedModelRequest else request
        if private.intellectual_state != snapshot["projection"]:
            raise ValueError("intellectual projection changed after admission")
        snapshot["dependencies"]["context:" + operation] = operation
        for (shared,) in db.execute(
            "SELECT shared_operation FROM protected_reply_shared WHERE operation_id=?",
            (operation,),
        ):
            for (event,) in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (shared,),
            ):
                snapshot["dependencies"]["event:" + event] = event
        from app.core.intellectual_state import encoded, MAX_DEPENDENCIES

        if len(snapshot["dependencies"]) > MAX_DEPENDENCIES:
            raise ValueError("intellectual dependency bound exceeded")
        db.execute(
            "UPDATE agent_intellectual_operations SET snapshot=?,context_binding=? WHERE audience=? AND owner=? AND operation=?",
            (
                encoded(snapshot),
                digest(json.loads(request.serialize())),
                *state.scope,
                operation,
            ),
        )

    def valid(self, principal, operation, *, revisions=True):
        with closing(self.app._event_store._open()) as db:
            return self.state(principal).validate(
                db,
                operation,
                self.sources(principal, db),
                self.controls(),
                revisions=revisions,
            )

    def commit(self, principal, operation, validation):
        with closing(self.app._event_store._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            caps = self.app._capabilities()
            if (
                caps.emergency_stop_active
                or caps.provider_spend_suspended
                or not caps.private_reply_drafts_enabled
                or not self.app._current_request.get()()
            ):
                return
            state = self.state(principal)
            if validation is not None and validation.intellectual_model is not None:
                state.receive(
                    db,
                    operation,
                    validation.intellectual,
                    validation.intellectual_model,
                    validation.intellectual_prompt,
                    self.app._clock().isoformat(),
                )
            row = state._operation(db, operation)
            if row is not None and row[5] == "received":
                state.apply(db, operation, self.sources(principal, db))
