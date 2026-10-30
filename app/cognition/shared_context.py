"""Closed offline v3 context under the existing complete serialized byte bound."""

from dataclasses import dataclass
import json

from app.core.model_contract import MAX_MODEL_REQUEST_UTF8_BYTES

from .conversation_context import MemoryModelRequest
from .shared import SharedRecord


@dataclass(frozen=True, slots=True)
class SharedModelRequest:
    private: MemoryModelRequest
    shared: tuple[SharedRecord, ...]
    shared_omitted: bool

    @property
    def conversation(self):
        return self.private.conversation

    def serialize(self):
        result = self._serialized()
        if len(result.encode("utf-8")) > MAX_MODEL_REQUEST_UTF8_BYTES:
            raise ValueError("shared request exceeds serialized byte bound")
        return result

    def _serialized(self):
        if (
            type(self.private) is not MemoryModelRequest
            or type(self.shared) is not tuple
            or len(self.shared) > 16
            or any(type(r) is not SharedRecord for r in self.shared)
            or type(self.shared_omitted) is not bool
        ):
            raise ValueError("invalid shared model request")
        data = json.loads(self.private.serialize())
        data["contract_version"] = (
            data["contract_version"]
            if self.private.narrative_mode
            else (
                "model-contract-conversation-transcript-v4"
                if self.private.conversation.contract_version
                == "model-contract-conversation-transcript-v4"
                else "model-contract-shared-intelligence-v3"
            )
        )
        data["shared_intellectual_state"] = [r.structured() for r in self.shared]
        data["shared_omitted"] = self.shared_omitted
        result = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return result


def assemble_shared(private, sources, shared, principal, db, budget):
    request = SharedModelRequest(private, (), False)
    if len(request.serialize().encode("utf-8")) > budget:
        raise ValueError("current input exceeds shared budget")
    dependencies = set(sources)
    selected = list(private.inherited_shared_operation_ids)
    for operation_id in selected:
        dependencies.update(
            row[0]
            for row in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (operation_id,),
            )
        )
    omitted = False
    # The private component has already authorized and ranked its own data. The
    # shared component applies audience/purpose before deterministic ordering.
    for record in shared.select(db, principal):
        if record.operation_id in selected:
            continue
        exposed_operations = {
            record.operation_id,
            *record.historical_reviewed_operation_ids,
        }
        evidence = {
            row[0]
            for shared_operation in exposed_operations
            for row in db.execute(
                "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                (shared_operation,),
            )
        }
        trial = SharedModelRequest(private, (*request.shared, record), omitted)
        if (
            len(dependencies | evidence) > 100
            or len(set(selected) | exposed_operations) > 16
            or len(trial._serialized().encode("utf-8"))
            > min(budget, MAX_MODEL_REQUEST_UTF8_BYTES)
        ):
            omitted = True
            continue
        dependencies.update(evidence)
        selected.extend(sorted(exposed_operations - set(selected)))
        request = trial
    return SharedModelRequest(private, request.shared, omitted), tuple(sorted(selected))
