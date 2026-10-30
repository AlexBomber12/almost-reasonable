"""Public account adapter for typed agent state and immutable inference inputs."""

import sqlite3

from app.core.intellectual_state import (
    IntellectualStateStore,
    initialize,
    digest,
    PROMPT_VERSION,
)


class PublicIntellectualState:
    def __init__(self, app):
        self.app = app
        self.state = IntellectualStateStore("public", app.account)
        with app.journal.transaction() as db:
            initialize(db)

    def sources(self, *, _candidates=None):
        result = {}
        candidates = (
            self.app.experience_history._candidates()
            if _candidates is None
            else _candidates
        )
        for key, (record, binding) in candidates.items():
            dependencies = {}
            if key.startswith(("decision:", "publication:")):
                with sqlite3.connect(self.app.journal.path) as db:
                    dependencies = self.state.dependencies(db, key.split(":", 1)[1])
            result[key] = {
                "kind": record.kind,
                "text": record.text,
                "binding": binding,
                "dependencies": dependencies,
            }
        import json
        from app.threads.store import ThreadsStoreError

        with sqlite3.connect(self.app.journal.path) as db:
            rows = db.execute(
                "SELECT operation,phase,payload FROM intents WHERE phase IN ('linked','rejected')"
            ).fetchall()
        for operation, phase, raw in rows:
            payload = json.loads(raw)
            if not self._origin_allowed(payload):
                continue
            if phase == "rejected":
                kind, text, binding = (
                    "decision_rejected",
                    "The application rejected this public decision; no action is confirmed.",
                    digest(payload),
                )
            else:
                try:
                    item = self.app.service.store.get(operation)
                except ThreadsStoreError:
                    continue
                if item.account_ref != self.app.account or item.state.value not in (
                    "unknown",
                    "failed_terminal",
                ):
                    continue
                kind = (
                    "publication_unknown"
                    if item.state.value == "unknown"
                    else "publication_failed"
                )
                text, binding = (
                    "Observed publication status: "
                    + item.state.value
                    + ". Completion is not confirmed.",
                    digest([payload, item.state.value, item.revision]),
                )
            with sqlite3.connect(self.app.journal.path) as db:
                deps = self.state.dependencies(db, operation)
            result["outcome:" + operation] = {
                "kind": kind,
                "text": text,
                "binding": binding,
                "dependencies": deps,
            }
        with sqlite3.connect(self.app.journal.path) as db:
            research = db.execute(
                "SELECT r.operation,r.evidence,i.payload FROM public_research_ops r JOIN intents i ON i.operation=r.operation WHERE r.phase='completed'"
            ).fetchall()
            for operation, evidence, raw in research:
                payload = json.loads(raw)
                if evidence is not None and self._origin_allowed(payload):
                    result["research:" + operation] = {
                        "kind": "hosted_research_model_report",
                        "text": "The full report in prior_untrusted_public_evidence is unverified model assessment, including its uncertainty and counterevidence; URLs are not factual approval.",
                        "binding": digest([payload, evidence]),
                        "dependencies": self.state.dependencies(db, operation),
                    }
        # Transitive protected bindings are flattened at admission, including sources
        # behind an earlier agent expression. A self-reference cannot restore them.
        for _ in range(len(result)):
            invalid = [
                key
                for key, source in result.items()
                if not self.state._valid_dependencies(source["dependencies"], result)
            ]
            if not invalid:
                break
            for key in invalid:
                del result[key]
        return result

    def _origin_allowed(self, payload):
        from app.integration.threads_stage0 import ThreadsStage0

        if payload.get(
            "generation"
        ) != self.app.ledger.control_generation() or not ThreadsStage0.source_valid(
            self.app, payload
        ):
            return False
        try:
            root = (
                None
                if self.app.mission is None
                else self.app.mission._root_snapshot()[1]
            )
            if payload.get("experience_mission_root", payload.get("mission")) != root:
                return False
        except ValueError:
            return False
        if payload["kind"] != "wake":
            from app.threads.domain import InboundKind

            event = self.app.service.store.inbound_event(
                self.app.account, InboundKind(payload["kind"]), payload["event"]
            )
            if event is None or not self.app.experience_history._event_allowed(event):
                return False
        return True

    @staticmethod
    def auxiliary_digest(catalog, evidence):
        return digest(
            {
                "version": "agent-intellectual-state-v1",
                "catalog": catalog,
                "evidence": evidence,
            }
        )

    def auxiliary_in(self, db, profile):
        app = self.app
        if profile is None:
            return (), (), None
        choices = app._available_topics_in(db, profile)
        catalog = profile.catalog(choices)
        pending = (
            None
            if app.mission_evolution is None
            else app.mission_evolution.pending_in(db)
        )
        evidence, source = app._selected_public_evidence_in(db, profile, pending)
        return catalog, evidence, source

    def auxiliary_matches(self, payload, catalog, evidence, source):
        return "intellectual_auxiliary_digest" not in payload or (
            payload["intellectual_auxiliary_digest"]
            == self.auxiliary_digest(catalog, evidence)
            and payload["intellectual_research_source"] == source
        )

    def context_dependencies(
        self, origin, sources, selected=(), *, research_source=None
    ):
        keys = {item["key"] for item in selected}
        if origin["kind"] != "wake":
            keys.add(f"event:{origin['kind']}:{origin['event']}")
        if research_source is not None:
            key = "research:" + research_source
            if key not in sources:
                raise ValueError("intellectual research context unavailable")
            keys.add(key)
        required = {}
        for key in keys:
            if key in sources:
                required.update(sources[key]["dependencies"])
                required[key] = sources[key]["binding"]
        return required

    def reserve(self, db, operation, origin, selected, sources):
        required = self.context_dependencies(
            origin,
            sources,
            selected,
            research_source=origin.get("intellectual_research_source"),
        )
        visible = {item["key"] for item in selected}
        report = origin.get("intellectual_research_source")
        priority = () if report is None else ("research:" + report,)
        visible.update(priority)
        if origin["kind"] != "wake":
            visible.add(f"event:{origin['kind']}:{origin['event']}")
        # Outcomes are optional new projection records, not already visible context.
        visible.update(
            sorted(
                (key for key in sources if key.startswith("outcome:")), reverse=True
            )[:2]
        )
        return self.state.reserve(
            db,
            operation,
            sources,
            digest(origin),
            str(self.app.ledger.control_generation()),
            visible_keys=visible,
            priority_keys=priority,
            required_dependencies=required,
            query=sources.get(f"event:{origin['kind']}:{origin['event']}", {}).get(
                "text", ""
            ),
        )

    def context(self, operation):
        app = self.app
        app.guard()
        sources = self.sources()
        with sqlite3.connect(app.journal.path) as db:
            if not self.state.validate(
                db, operation, sources, str(app.ledger.control_generation())
            ):
                raise ValueError("public intellectual state changed")
            row = self.state._operation(db, operation)
            if row is None:
                return None
            import json

            return json.loads(row[2])["projection"]

    def valid(self, operation, *, revisions=True):
        sources = self.sources()
        with sqlite3.connect(self.app.journal.path) as db:
            return self.state.validate(
                db,
                operation,
                sources,
                str(self.app.ledger.control_generation()),
                revisions=revisions,
            )

    def commit(self, operation):
        app = self.app
        # Existing short fences serialize public source/control mutation. Independent
        # stores remain independent; no provider work runs inside these fences.
        with (
            app.service.store.conversation_control_fence(),
            app.ledger._open() as ledger_db,
        ):
            ledger_db.execute("BEGIN IMMEDIATE")
            with app.journal.transaction() as db:
                app.guard()
                import json

                current_payload = json.loads(
                    db.execute(
                        "SELECT payload FROM intents WHERE operation=?", (operation,)
                    ).fetchone()[0]
                )
                app.require_mission(current_payload)
                profile = app.current_research_profile()
                catalog, evidence, source = self.auxiliary_in(db, profile)
                if not self.auxiliary_matches(
                    current_payload, catalog, evidence, source
                ):
                    raise ValueError("intellectual auxiliary context changed")
                sources = self.sources()
                if not self.state.validate(
                    db, operation, sources, str(app.ledger.control_generation())
                ):
                    raise ValueError("public intellectual state changed")
                result = app.model.intellectual_results.pop(operation, None)
                if result is not None:
                    proposal, model = result
                    self.state.receive(
                        db,
                        operation,
                        proposal,
                        model,
                        PROMPT_VERSION,
                        app.clock.now().isoformat(),
                    )
                row = self.state._operation(db, operation)
                if row is not None and row[5] == "received":
                    app.guard()
                    self.state.apply(db, operation, sources)
                app.require_mission(current_payload)
