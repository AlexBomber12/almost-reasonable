"""Participant-scoped fictional episodes, separate from events and factual state."""

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
import sqlite3

from app.core.events import TrustedPrincipalContext
from app.core.store import EventStore

MAX_EPISODES = 16
MAX_REVISIONS = 16
MAX_EPISODE_BYTES = 4096
MAX_NARRATIVE_GRAPH_REPLIES = 65536
MAX_NARRATIVE_RECEIPTS = 4096


class NarrativeConflict(ValueError):
    """The proposed episode no longer has its trusted source binding."""


@dataclass(frozen=True, slots=True)
class Episode:
    operation_id: str
    item_id: str
    revision: int
    text: str
    sources: tuple[str, ...]
    created_at: str


def _eligible(
    db: sqlite3.Connection, principal: str, operation: str
) -> tuple[str, ...] | None:
    rows = db.execute(
        """SELECT s.event_id,e.audience_principal_id,e.event_type,e.channel,
                  e.provenance_type,e.verification_status,p.retention_status,
                  p.audience_principal_id,t.source_event_id,t.source_principal_id
           FROM narrative_sources s JOIN event_envelopes e ON e.event_id=s.event_id
           LEFT JOIN private_payloads p ON p.payload_id=e.payload_id
           LEFT JOIN protected_event_taints t ON t.event_id=e.event_id
           WHERE s.principal_id=? AND s.operation_id=? AND s.retention_status='active'
           ORDER BY s.event_id""",
        (principal, operation),
    ).fetchall()
    count = db.execute(
        "SELECT source_count FROM narrative_operations WHERE principal_id=? AND operation_id=? AND status='active'",
        (principal, operation),
    ).fetchone()
    if count is None or len(rows) != count[0] or not rows:
        return None
    if any(
        row[1:]
        != (
            principal,
            "participant_input",
            "private_chat",
            "participant_assertion",
            "unverified",
            "active",
            principal,
            row[0],
            principal,
        )
        for row in rows
    ):
        return None
    return tuple(row[0] for row in rows)


class NarrativeStore:
    def __init__(self, events: EventStore):
        self._events = events

    def current(
        self, context: TrustedPrincipalContext, *, _connection=None
    ) -> tuple[Episode, ...]:
        EventStore._validate_context(context)
        db = _connection if _connection is not None else self._events._open()
        try:
            self._events._verify_current_schema(db)
            principal = context.principal_id.value
            rows = db.execute(
                """SELECT o.operation_id,o.item_id,o.revision,p.episode,o.created_at
                   FROM narrative_items i JOIN narrative_operations o
                     ON o.principal_id=i.principal_id AND o.item_id=i.item_id
                     AND o.revision=i.current_revision
                   JOIN narrative_payloads p
                     ON p.principal_id=o.principal_id AND p.operation_id=o.operation_id
                   WHERE i.principal_id=? AND o.status='active'
                   ORDER BY o.created_at DESC,o.operation_id DESC""",
                (principal,),
            ).fetchall()
            result = []
            for operation, item, revision, text, created in rows:
                sources = _eligible(db, principal, operation)
                if sources is not None:
                    result.append(
                        Episode(operation, item, revision, text, sources, created)
                    )
            return tuple(result)
        finally:
            if _connection is None:
                db.close()

    def committed(
        self,
        context: TrustedPrincipalContext,
        *,
        operation_id: str,
        source_event_id: str,
        proposal,
        selected: tuple[tuple[str, str], ...],
        heads: tuple[tuple[str, int], ...],
        sources: tuple[str, ...],
    ) -> bool:
        """Recognize only the exact durable operation after a post-commit crash."""
        EventStore._validate_context(context)
        if proposal is None or proposal.decision not in {"create", "revise"}:
            return False
        principal = context.principal_id.value
        db = self._events._open()
        try:
            self._events._verify_current_schema(db)
            row = db.execute(
                """SELECT o.item_id,o.revision,o.expected_revision,o.status,p.episode
                   FROM narrative_operations o JOIN narrative_payloads p
                     ON p.principal_id=o.principal_id AND p.operation_id=o.operation_id
                   WHERE o.principal_id=? AND o.operation_id=?""",
                (principal, operation_id),
            ).fetchone()
            if (
                row is None
                or row[3] != "active"
                or row[4] != proposal.episode
                or _eligible(db, principal, operation_id) != sources
                or source_event_id not in sources
            ):
                return False
            contexts = tuple(
                item[0]
                for item in db.execute(
                    """SELECT context_operation_id FROM narrative_context
                       WHERE principal_id=? AND operation_id=?
                       ORDER BY context_operation_id""",
                    (principal, operation_id),
                )
            )
            if contexts != tuple(sorted(operation for _handle, operation in selected)):
                return False
            if proposal.decision == "create":
                expected_item = "episode:" + operation_id.removeprefix("narrative:")
                expected_revision = 0
                if any(item == expected_item for item, _revision in heads):
                    return False
            else:
                target = dict(selected).get(proposal.handle)
                if target is None:
                    return False
                previous = db.execute(
                    """SELECT item_id,revision FROM narrative_operations
                       WHERE principal_id=? AND operation_id=?""",
                    (principal, target),
                ).fetchone()
                if previous is None or previous not in heads:
                    return False
                expected_item, expected_revision = previous
            return row[:3] == (
                expected_item,
                expected_revision + 1,
                expected_revision,
            )
        finally:
            db.close()

    def commit(
        self,
        context: TrustedPrincipalContext,
        *,
        operation_id: str,
        source_event_id: str,
        text: str,
        item_id: str,
        expected_revision: int,
        expected_heads: tuple[tuple[str, int], ...],
        exposed_sources: tuple[str, ...],
        exposed_episodes: tuple[str, ...],
        created_at: datetime,
        _connection: sqlite3.Connection | None = None,
    ) -> Episode:
        """CAS against all visible heads and the complete model-visible source union."""
        EventStore._validate_context(context)
        for label, value in (
            ("operation", operation_id),
            ("source", source_event_id),
            ("item", item_id),
        ):
            EventStore._require_token(label, value)
        if (
            type(text) is not str
            or not text.strip()
            or len(text.encode("utf-8")) > MAX_EPISODE_BYTES
            or type(expected_revision) is not int
            or not 0 <= expected_revision < MAX_REVISIONS
            or type(expected_heads) is not tuple
            or type(exposed_sources) is not tuple
            or type(exposed_episodes) is not tuple
            or type(created_at) is not datetime
            or created_at.tzinfo is None
        ):
            raise NarrativeConflict("invalid narrative proposal")
        principal = context.principal_id.value
        db = _connection if _connection is not None else self._events._open()
        try:
            with db if _connection is None else nullcontext(db):
                if _connection is None:
                    db.execute("BEGIN IMMEDIATE")
                self._events._verify_current_schema(db)
                existing = db.execute(
                    "SELECT item_id,revision FROM narrative_operations WHERE principal_id=? AND operation_id=?",
                    (principal, operation_id),
                ).fetchone()
                if existing is not None:
                    raise NarrativeConflict("narrative operation replay")
                current = self.current(context, _connection=db)
                heads = tuple(
                    sorted((episode.item_id, episode.revision) for episode in current)
                )
                if heads != expected_heads:
                    raise NarrativeConflict("stale narrative heads")
                visible = {episode.operation_id: episode for episode in current}
                if (
                    len(set(exposed_episodes)) != len(exposed_episodes)
                    or not set(exposed_episodes) <= visible.keys()
                ):
                    raise NarrativeConflict("invalid narrative context")
                revision_row = db.execute(
                    "SELECT current_revision FROM narrative_items WHERE principal_id=? AND item_id=?",
                    (principal, item_id),
                ).fetchone()
                if (revision_row[0] if revision_row else 0) != expected_revision:
                    raise NarrativeConflict("stale narrative revision")
                if revision_row is not None and not any(
                    episode.item_id == item_id for episode in current
                ):
                    raise NarrativeConflict("narrative head unavailable")
                if revision_row is None and len(current) >= MAX_EPISODES:
                    raise NarrativeConflict("narrative capacity reached")
                if db.execute("SELECT COUNT(*) FROM narrative_operations").fetchone()[
                    0
                ] >= 4096 or (
                    revision_row is None
                    and db.execute("SELECT COUNT(*) FROM narrative_items").fetchone()[0]
                    >= 256
                ):
                    raise NarrativeConflict("narrative storage capacity reached")
                sources = set(exposed_sources) | {source_event_id}
                for episode_id in exposed_episodes:
                    sources.update(visible[episode_id].sources)
                if (
                    not 1 <= len(sources) <= 100
                    or tuple(sorted(set(exposed_sources))) != exposed_sources
                ):
                    raise NarrativeConflict("invalid narrative sources")
                for source in sources:
                    EventStore._require_token("narrative source", source)
                    row = db.execute(
                        """SELECT e.audience_principal_id,e.event_type,e.channel,
                                  e.provenance_type,e.verification_status,p.retention_status,
                                  p.audience_principal_id,t.source_event_id,t.source_principal_id
                           FROM event_envelopes e JOIN private_payloads p USING(payload_id)
                           JOIN protected_event_taints t ON t.event_id=e.event_id
                           WHERE e.event_id=?""",
                        (source,),
                    ).fetchone()
                    if row != (
                        principal,
                        "participant_input",
                        "private_chat",
                        "participant_assertion",
                        "unverified",
                        "active",
                        principal,
                        source,
                        principal,
                    ):
                        raise NarrativeConflict("narrative source unavailable")
                if revision_row is None:
                    db.execute(
                        "INSERT INTO narrative_items VALUES (?,?,1)",
                        (principal, item_id),
                    )
                else:
                    db.execute(
                        "UPDATE narrative_items SET current_revision=? WHERE principal_id=? AND item_id=?",
                        (expected_revision + 1, principal, item_id),
                    )
                revision = expected_revision + 1
                db.execute(
                    "INSERT INTO narrative_operations VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        principal,
                        operation_id,
                        item_id,
                        revision,
                        expected_revision,
                        "active",
                        len(sources),
                        len(exposed_episodes),
                        created_at.isoformat(),
                    ),
                )
                db.execute(
                    "INSERT INTO narrative_payloads VALUES (?,?,?)",
                    (principal, operation_id, text),
                )
                db.executemany(
                    "INSERT INTO narrative_sources VALUES (?,?,?,?)",
                    (
                        (principal, operation_id, source, "active")
                        for source in sorted(sources)
                    ),
                )
                db.executemany(
                    "INSERT INTO narrative_context VALUES (?,?,?)",
                    (
                        (principal, operation_id, episode)
                        for episode in exposed_episodes
                    ),
                )
                return Episode(
                    operation_id,
                    item_id,
                    revision,
                    text,
                    tuple(sorted(sources)),
                    created_at.isoformat(),
                )
        finally:
            if _connection is None:
                db.close()


def erase_narrative_source(db: sqlite3.Connection, event_id: str) -> None:
    """Erase only episodes and dependent replies touched by this private source."""
    rows = db.execute(
        "SELECT principal_id,operation_id FROM narrative_sources WHERE event_id=?",
        (event_id,),
    ).fetchall()
    for principal, operation in rows:
        db.execute(
            "DELETE FROM narrative_payloads WHERE principal_id=? AND operation_id=?",
            (principal, operation),
        )
        db.execute(
            "UPDATE narrative_operations SET status='invalidated' WHERE principal_id=? AND operation_id=?",
            (principal, operation),
        )
        db.execute(
            "UPDATE narrative_sources SET retention_status='invalidated' WHERE principal_id=? AND operation_id=?",
            (principal, operation),
        )
        replies = db.execute(
            "SELECT reply_operation_id FROM protected_reply_narrative WHERE principal_id=? AND narrative_operation_id=?",
            (principal, operation),
        ).fetchall()
        for (reply,) in replies:
            db.execute(
                "DELETE FROM agent_reply_payloads WHERE operation_id=?", (reply,)
            )
            db.execute(
                "UPDATE agent_reply_operations SET retention_status='invalidated' WHERE operation_id=?",
                (reply,),
            )
            db.execute(
                "UPDATE agent_reply_sources SET retention_status='invalidated' WHERE operation_id=?",
                (reply,),
            )


def validate_narrative_graph(db: sqlite3.Connection) -> None:
    """Fail closed on malformed protected episode and reply dependency graphs."""
    bounds = (
        ("narrative_items", 256),
        ("narrative_operations", 4096),
        ("narrative_sources", 409600),
        ("narrative_context", 65536),
        ("protected_reply_narrative", MAX_NARRATIVE_GRAPH_REPLIES),
    )
    for table, maximum in bounds:
        if db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > maximum:
            raise ValueError("invalid narrative graph")
    items = {
        (principal, item): head
        for principal, item, head in db.execute(
            "SELECT principal_id,item_id,current_revision FROM narrative_items"
        )
    }
    operations = {}
    for (
        principal,
        operation,
        item,
        revision,
        expected,
        status,
        source_count,
        context_count,
    ) in db.execute(
        "SELECT principal_id,operation_id,item_id,revision,expected_revision,status,source_count,context_count FROM narrative_operations"
    ):
        if (
            (principal, item) not in items
            or revision != expected + 1
            or (principal, operation) in operations
        ):
            raise ValueError("invalid narrative graph")
        operations[(principal, operation)] = (
            item,
            revision,
            status,
            source_count,
            context_count,
        )
    for (principal, item), head in items.items():
        revisions = sorted(
            row[1]
            for key, row in operations.items()
            if key[0] == principal and row[0] == item
        )
        if revisions != list(range(1, head + 1)):
            raise ValueError("invalid narrative graph")
    payloads = {
        (principal, operation)
        for principal, operation in db.execute(
            "SELECT principal_id,operation_id FROM narrative_payloads"
        )
    }
    for key, (
        item,
        revision,
        status,
        source_count,
        context_count,
    ) in operations.items():
        principal, operation = key
        sources = db.execute(
            "SELECT event_id,retention_status FROM narrative_sources WHERE principal_id=? AND operation_id=?",
            key,
        ).fetchall()
        contexts = db.execute(
            "SELECT context_operation_id FROM narrative_context WHERE principal_id=? AND operation_id=?",
            key,
        ).fetchall()
        if (
            len(sources) != source_count
            or len(contexts) != context_count
            or (key in payloads) != (status == "active")
            or any(row[1] != status for row in sources)
        ):
            raise ValueError("invalid narrative graph")
        if status == "active" and _eligible(db, principal, operation) is None:
            raise ValueError("invalid narrative graph")
        source_ids = {row[0] for row in sources}
        for (context_operation,) in contexts:
            inherited = operations.get((principal, context_operation))
            if inherited is None or inherited[1] >= revision and inherited[0] == item:
                raise ValueError("invalid narrative graph")
            parent_sources = {
                row[0]
                for row in db.execute(
                    "SELECT event_id FROM narrative_sources WHERE principal_id=? AND operation_id=?",
                    (principal, context_operation),
                )
            }
            if not parent_sources <= source_ids:
                raise ValueError("invalid narrative graph")
    for reply, principal, operation in db.execute(
        "SELECT reply_operation_id,principal_id,narrative_operation_id FROM protected_reply_narrative"
    ):
        narrative = operations.get((principal, operation))
        row = db.execute(
            "SELECT audience_principal_id,retention_status,narrative_count FROM agent_reply_operations WHERE operation_id=?",
            (reply,),
        ).fetchone()
        if narrative is None or row is None or row[0] != principal:
            raise ValueError("invalid narrative graph")
        if row[1] == "active" and narrative[2] != "active":
            raise ValueError("invalid narrative graph")
        sources = {
            event
            for (event,) in db.execute(
                "SELECT event_id FROM narrative_sources WHERE principal_id=? AND operation_id=?",
                (principal, operation),
            )
        }
        reply_sources = {
            event
            for (event,) in db.execute(
                "SELECT event_id FROM agent_reply_sources WHERE operation_id=?",
                (reply,),
            )
        }
        if not sources <= reply_sources:
            raise ValueError("invalid narrative graph")
    if (
        db.execute(
            """SELECT 1 FROM agent_reply_operations r
           LEFT JOIN protected_reply_narrative n
             ON n.reply_operation_id=r.operation_id
           GROUP BY r.operation_id
           HAVING COUNT(n.narrative_operation_id) != r.narrative_count
           LIMIT 1"""
        ).fetchone()
        is not None
    ):
        raise ValueError("invalid narrative graph")
