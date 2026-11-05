"""Admission for whole optional records before protected ancestry overflows."""

import json

from app.cognition.shared_context import SharedModelRequest
from app.core.intellectual_state import MAX_DEPENDENCIES, encoded
from app.core.public_intellectual_receipt import MAX_PROTECTED_BYTES


def context_fits(
    app,
    db,
    principal,
    operation,
    request,
    event_ids,
    *,
    creator_cache,
    shared_cache,
    narrative_slot=False,
):
    private = request.private if type(request) is SharedModelRequest else request
    dependencies = {"event:" + event: event for event in event_ids}
    dependencies["context:" + operation] = operation
    receipts = {}
    groups = [
        set(private.inherited_shared_operation_ids),
        set(private.generic_operation_ids),
        set(private.narrative_operation_ids),
    ]
    state = app._intellectual.state(principal)
    creators = set(private.released_reply_ids) | {
        "reply:" + episode.removeprefix("narrative:")
        for episode in private.projected_narrative_operation_ids
    }
    for creator in creators:
        if creator not in creator_cache:
            lineage = state.dependencies(db, creator)
            reply = app._outputs._read(db, principal, creator)
            inherited_receipts, inherited_groups = {}, ((), (), ())
            # Standalone fiction can have no reply creator; its own roots are
            # already admitted. Cache only metadata within this SQL transaction.
            if reply is not None:
                lineage["context:" + creator] = creator
                if app._research_evidence(creator) is not None:
                    lineage["reply:" + creator] = "release:" + creator
                row = state._operation(db, creator)
                if row is not None and row[2] is not None:
                    inherited_receipts = json.loads(row[2]).get("external_receipts", {})
                inherited_groups = (
                    reply.operation.shared_operation_ids,
                    reply.operation.generic_operation_ids,
                    reply.operation.narrative_operation_ids,
                )
            creator_cache[creator] = lineage, inherited_receipts, inherited_groups
        lineage, inherited_receipts, inherited_groups = creator_cache[creator]
        dependencies.update(lineage)
        receipts.update(inherited_receipts)
        for group, values in zip(
            groups,
            inherited_groups,
            strict=True,
        ):
            group.update(values)
    if type(request) is SharedModelRequest:
        for record in request.shared:
            groups[0].update(
                (record.operation_id, *record.historical_reviewed_operation_ids)
            )
    for receipt in receipts.values():
        groups[0].update(receipt["shared"])
    for shared in groups[0]:
        if shared not in shared_cache:
            shared_cache[shared] = tuple(
                row[0]
                for row in db.execute(
                    "SELECT event_id FROM protected_shared_sources WHERE operation_id=?",
                    (shared,),
                )
            )
        for event in shared_cache[shared]:
            dependencies["event:" + event] = event
    # Keep bounded headroom for a complete optional public projection. Its actual
    # source ancestry is still checked by attach(); no dependency is truncated.
    public = app._public_intellectual_source is not None
    return (
        len(dependencies) <= MAX_DEPENDENCIES - (6 if public else 0)
        and len(
            encoded(
                {"dependencies": dependencies, "external_receipts": receipts}
            ).encode()
        )
        <= MAX_PROTECTED_BYTES - (8192 if public else 0)
        and all(
            len(group) <= limit
            for group, limit in zip(
                groups, (16, 16, 15 if narrative_slot else 16), strict=True
            )
        )
    )
