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

    def source_validator(self, db, principal):
        """Recheck report ancestry without binding unrelated inference pipelines."""
        state, sources = (
            self.state(principal),
            self.sources(principal, db),
        )
        checked = {}

        def permitted(creator):
            if creator not in checked:
                reply = self.app._outputs._read(db, principal, creator)
                deps = state.dependencies(db, creator)
                report_ancestry = any(
                    key.startswith("reply:")
                    and self.app._research_evidence(key.removeprefix("reply:"))
                    is not None
                    for key in deps
                )
                checked[creator] = (
                    (
                        reply is None
                        or self.app._historical_research_permitted(principal, reply)
                    )
                    and (
                        not report_ancestry or state._valid_dependencies(deps, sources)
                    )
                    and self.app._public_intellectual_dependencies_live(
                        creator, principal
                    )
                )
            return checked[creator]

        return permitted

    def research_valid(self, principal, operation):
        """Narrative sees transcript/fiction, not the separate intellectual heads."""
        with closing(self.app._event_store._open()) as db:
            return self.source_validator(db, principal)(operation)

    def public_sources(self, principal, db, *, required_keys=None):
        source = getattr(self.app, "_public_intellectual_source", None)
        return (
            {}
            if source is None
            else source.sources(
                principal=principal, _private_db=db, required_keys=required_keys
            )
        )

    def sources(self, principal, db, *, _public_selection=None):
        app = self.app
        result = dict(
            self.public_sources(principal, db)
            if _public_selection is None
            else _public_selection
        )
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
                required = frozenset(
                    key
                    for key in self.state(principal).dependencies(db, operation)
                    if key.startswith("public-state:")
                )
                if required:
                    result.update(
                        self.public_sources(principal, db, required_keys=required)
                    )
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
            research = app._research_evidence(operation) is not None
            text = reply.content
            if research:
                from .research_questions import read

                text = "The complete untrusted hosted research report is in this call's conversation. Its uncertainty, contradictions and source relationships are model assessments, not independent factual verification."
                question = read(db, operation)
                if question is not None:
                    text += " Minimized research question: " + question["question"]
            result["reply:" + operation] = {
                "kind": "hosted_research_model_report"
                if research
                else "server_released_agent_response",
                "text": text,
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

    def attach(
        self,
        db,
        principal,
        operation,
        request,
        event_ids,
        *,
        operation_groups=None,
        reserve_narrative_slot=False,
    ):
        state = self.state(principal)
        public_selection = self.public_sources(principal, db)
        sources = self.sources(principal, db, _public_selection=public_selection)
        visible = {"event:" + event for event in event_ids}
        visible.update(public_selection)
        visible.update(
            sorted(
                (
                    key
                    for key in sources
                    if key.startswith("reply:")
                    and sources[key]["kind"] != "hosted_research_model_report"
                ),
                reverse=True,
            )[:3]
        )
        private = request.private if type(request) is SharedModelRequest else request
        # Only complete reports actually admitted to this transcript get handles.
        priority = tuple(
            "reply:" + creator
            for creator in private.released_reply_ids
            if "reply:" + creator in sources
            and sources["reply:" + creator]["kind"] == "hosted_research_model_report"
        )[:1]
        visible.update(priority)
        budget = (
            self.app._limits.max_utf8_bytes - len(request.serialize().encode()) - 64
        )

        def omit_optional_context():
            # Without a complete durable ancestry receipt, optional expressions
            # cannot be material context for a new reply or memory transition.
            if (
                not private.memories
                and not private.episodes
                and not any(
                    s.role.value == "agent" for s in private.conversation.conversation
                )
            ):
                return request, event_ids
            trial = replace(
                private,
                memories=(),
                episodes=(),
                projected_narrative_operation_ids=(),
                memories_omitted=True,
                released_reply_ids=(),
                conversation=replace(
                    private.conversation,
                    conversation=tuple(
                        s
                        for s in private.conversation.conversation
                        if s.role.value == "participant"
                    ),
                    history_omitted=True,
                ),
            )
            return (
                replace(request, private=trial)
                if type(request) is SharedModelRequest
                else trial
            ), event_ids

        if budget < 128:
            return omit_optional_context()
        from app.core.intellectual_state import IntellectualCapacityError

        required = {"event:" + event: event for event in event_ids}
        required["context:" + operation] = operation
        # Protect every admitted expression and derived fiction, even when only
        # one full research report has a selectable handle.
        creators = set(private.released_reply_ids) | {
            "reply:" + episode.removeprefix("narrative:")
            for episode in private.projected_narrative_operation_ids
        }
        for creator in creators:
            required.update(state.dependencies(db, creator))
            if "context:" + creator in sources:
                required["context:" + creator] = creator
            key = "reply:" + creator
            if (
                key in sources
                and sources[key]["kind"] == "hosted_research_model_report"
            ):
                required.update(sources[key]["dependencies"])
                required[key] = sources[key]["binding"]
        shared = list(private.inherited_shared_operation_ids)
        if type(request) is SharedModelRequest:
            shared.extend(record.operation_id for record in request.shared)
        for shared_operation in shared:
            for (event,) in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (shared_operation,),
            ):
                required["event:" + event] = event
        base_groups = operation_groups or (
            shared,
            private.generic_operation_ids,
            private.narrative_operation_ids,
        )
        limits = (16, 16, 15 if reserve_narrative_slot else 16)

        def dependencies_fit(dependencies):
            inherited = self.inherited_operations(
                db, principal, operation, dependencies=dependencies, sources=sources
            )
            from app.core.public_intellectual_receipt import collect
            from app.core.intellectual_state import encoded

            protected = {
                "dependencies": dependencies,
                "external_receipts": collect(dependencies, sources),
            }
            return len(encoded(protected).encode()) <= 45056 and all(
                len(set(base) | extra) <= limit
                for base, extra, limit in zip(base_groups, inherited, limits)
            )

        try:
            value = state.reserve(
                db,
                operation,
                sources,
                digest(json.loads(request.serialize())),
                self.controls(),
                visible_keys=visible,
                priority_keys=priority,
                budget=budget,
                required_dependencies=required,
                query=private.conversation.conversation[-1].content,
                dependencies_fit=dependencies_fit,
            )
        except IntellectualCapacityError:
            return omit_optional_context()
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

    def inherited_operations(
        self, db, principal, operation, *, dependencies=None, sources=None
    ):
        groups = [set(), set(), set()]
        if dependencies is None:
            dependencies = self.state(principal).dependencies(db, operation)
        if sources is None:
            row = self.state(principal)._operation(db, operation)
            receipts = (
                {}
                if row is None or row[2] is None
                else json.loads(row[2]).get("external_receipts", {})
            )
        else:
            from app.core.public_intellectual_receipt import collect

            receipts = collect(dependencies, sources)
        for receipt in receipts.values():
            groups[0].update(receipt["shared"])
        for key in dependencies:
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
