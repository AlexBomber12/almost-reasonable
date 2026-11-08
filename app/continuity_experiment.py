"""Isolated, operator-driven shadow laboratory. No network or production imports."""

from contextlib import closing, contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from time import monotonic


VERSION = "ar-continuity-lab-v1"
APPLICATION_ID = 0x41524C31
MAX_FRAME_BYTES = 32768
MAX_STATES = 24
KINDS = {"message", "post", "comment", "research", "wake"}
ACTIONS = {"reply", "draft", "research", "no_action"}
STATE_KINDS = {"position", "question", "interest", "goal"}


class LabError(ValueError):
    """A bounded, payload-free diagnostic code."""


class ModelFailure(LabError):
    """A model boundary's sanitized failure, never an intentional no_action."""


def _text(value, maximum=2048, *, empty=False):
    if (
        type(value) is not str
        or len(value.encode()) > maximum
        or (not empty and not value.strip())
    ):
        raise LabError("invalid_text")
    return value


def _id(value):
    if type(value) is not str or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) is None:
        raise LabError("invalid_id")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(path):
    try:
        info = path.lstat()
    except OSError:
        raise LabError("unsafe_storage") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LabError("unsafe_storage")
    return info.st_dev, info.st_ino


class Lab:
    """One private new directory, a journal and a crash-released SQLite mutex.

    Public intent text is never sent to a platform. Participants here are explicit
    operator-selected scopes, not a replacement for production authentication.
    """

    @classmethod
    def create(cls, root, *, participants=("a", "b")):
        root = Path(root).absolute()
        participants = tuple(_id(p) for p in participants)
        if (
            len(participants) != 2
            or len(set(participants)) != 2
            or "public" in participants
        ):
            raise LabError("invalid_participants")
        if root.parent.resolve(strict=True) != root.parent:
            raise LabError("unsafe_directory")
        root.mkdir(mode=0o700)
        for name in ("lab.sqlite", "mutex.sqlite"):
            os.close(os.open(root / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with closing(sqlite3.connect(root / "lab.sqlite")) as db, db:
            db.executescript(
                f"PRAGMA application_id={APPLICATION_ID};"
                "CREATE TABLE metadata(version TEXT,participants TEXT);"
                "CREATE TABLE events(id TEXT PRIMARY KEY,scope TEXT,kind TEXT,text TEXT,source TEXT,parent TEXT);"
                "CREATE TABLE operations(id TEXT PRIMARY KEY,event TEXT,scope TEXT,model_kind TEXT,status TEXT,frame TEXT,result TEXT);"
                "CREATE TABLE revisions(scope TEXT,key TEXT,revision INTEGER,payload TEXT,operation TEXT,"
                "PRIMARY KEY(scope,key,revision));"
                "CREATE INDEX event_scope ON events(scope,kind);"
                "CREATE INDEX operation_scope ON operations(scope,status);"
            )
            db.execute(
                "INSERT INTO metadata VALUES (?,?)", (VERSION, _json(participants))
            )
        return cls(root)

    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.resolve(strict=True) != self.root:
            raise LabError("unsafe_directory")
        self.path = self.root / "lab.sqlite"
        self.identities = {
            p: _identity(p) for p in (self.path, self.root / "mutex.sqlite")
        }
        try:
            with self._open() as db:
                if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                    raise LabError("not_experiment")
                version, participants = db.execute(
                    "SELECT version,participants FROM metadata"
                ).fetchone()
                if version != VERSION:
                    raise LabError("not_experiment")
                self.participants = tuple(json.loads(participants))
        except sqlite3.Error:
            raise LabError("not_experiment") from None

    def _check(self):
        if any(
            _identity(path) != identity for path, identity in self.identities.items()
        ):
            raise LabError("storage_changed")

    def _open(self):
        self._check()
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0)
        db.row_factory = sqlite3.Row
        return closing(db)

    @contextmanager
    def _exclusive(self):
        self._check()
        with closing(
            sqlite3.connect(
                (self.root / "mutex.sqlite").as_uri() + "?mode=rw", uri=True, timeout=0
            )
        ) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                raise LabError("busy") from None
            try:
                yield
            finally:
                db.rollback()

    def _scope(self, scope):
        if scope not in ("public", *self.participants):
            raise LabError("invalid_scope")
        return scope

    def add(self, key, scope, kind, text, *, source="", parent=None):
        _id(key)
        self._scope(scope)
        _text(text)
        if kind not in KINDS or (scope != "public" and kind != "message"):
            raise LabError("invalid_event_kind")
        _text(source, 512, empty=kind in {"message", "wake"})
        with self._exclusive(), self._open() as db, db:
            if kind == "research":
                row = db.execute(
                    "SELECT result FROM operations WHERE id=? AND status='decided'",
                    (parent,),
                ).fetchone()
                result = None if row is None else json.loads(row[0])
                if (
                    result is None
                    or result["scope"] != scope
                    or result["decision"]["action"] != "research"
                ):
                    raise LabError("research_parent_missing")
            elif parent is not None:
                raise LabError("unexpected_parent")
            values = (key, scope, kind, text, source, parent)
            old = db.execute("SELECT * FROM events WHERE id=?", (key,)).fetchone()
            if old is not None:
                if tuple(old) != values:
                    raise LabError("event_conflict")
                return
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", values)

    @staticmethod
    def _states(db, scope):
        rows = db.execute(
            "SELECT payload FROM revisions r WHERE scope IN ('public',?) AND revision="
            "(SELECT MAX(revision) FROM revisions x WHERE x.scope=r.scope AND x.key=r.key) "
            "ORDER BY scope,key",
            (scope,),
        )
        result = []
        for row in rows:
            item = json.loads(row[0])
            previous = db.execute(
                "SELECT payload FROM revisions WHERE scope=? AND key=? AND revision=?",
                (item["scope"], item["key"], item["revision"] - 1),
            ).fetchone()
            if previous is not None:
                item["previous"] = json.loads(previous[0])
            result.append(item)
        return result

    def _context(self, db, event):
        trigger = db.execute("SELECT * FROM events WHERE id=?", (event,)).fetchone()
        if trigger is None:
            raise LabError("event_missing")
        trigger = dict(trigger)
        scope = trigger["scope"]
        memory = self._states(db, scope)
        # Bounded working memory is explicit. No silent eviction of positions.
        if len(memory) > MAX_STATES:
            raise LabError("memory_full")
        experience = {
            name: [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE scope='public' AND kind=? ORDER BY rowid DESC LIMIT 2",
                    (kind,),
                )
            ]
            for name, kind in [
                ("posts", "post"),
                ("comments", "comment"),
                ("research", "research"),
            ]
        }
        recent = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM events WHERE scope IN ('public',?) ORDER BY rowid DESC LIMIT 4",
                (scope,),
            )
        ]
        selected = {e["id"]: e for group in experience.values() for e in group}
        selected.update((e["id"], e) for e in recent)
        selected[event] = trigger
        for item in memory:
            dependencies = item["sources"] + item.get("previous", {}).get("sources", [])
            for source in dependencies:
                selected[source] = dict(
                    db.execute(
                        "SELECT * FROM events WHERE id=? AND scope IN ('public',?)",
                        (source, scope),
                    ).fetchone()
                )
        prior = []
        for row in db.execute(
            "SELECT result FROM operations WHERE status='decided' AND scope IN ('public',?) ORDER BY rowid DESC LIMIT 3",
            (scope,),
        ):
            result = json.loads(row[0])
            prior.append(
                dict(
                    operation=result["operation"],
                    decision=result["decision"],
                    effect="shadow_only",
                )
            )
            for source in result["decision"]["based_on"]:
                selected[source] = dict(
                    db.execute(
                        "SELECT * FROM events WHERE id=? AND scope IN ('public',?)",
                        (source, scope),
                    ).fetchone()
                )
        total = db.execute(
            "SELECT COUNT(*) FROM events WHERE scope IN ('public',?)", (scope,)
        ).fetchone()[0]
        frame = dict(
            version=VERSION,
            identity="Almost Reasonable — ИИ-агент",
            scope=scope,
            trigger=trigger,
            memory=memory,
            experience=experience,
            recent=recent,
            sources=list(selected.values()),
            decisions=prior,
            omitted_events=total - len(selected),
            history_complete=total == len(selected),
            authority="shadow_only; no publishing or tool execution",
        )
        if len(_json(frame).encode()) > MAX_FRAME_BYTES:
            raise LabError("context_full")
        return frame

    def context(self, event):
        with self._open() as db:
            return self._context(db, event)

    def inspect(self, scope):
        self._scope(scope)
        with self._open() as db:
            results = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT result FROM operations WHERE result IS NOT NULL ORDER BY rowid"
                )
            ]
            return dict(
                version=VERSION,
                scope=scope,
                memory=self._states(db, scope),
                event_count=db.execute(
                    "SELECT COUNT(*) FROM events WHERE scope IN ('public',?)", (scope,)
                ).fetchone()[0],
                operations=[r for r in results if r["scope"] in {"public", scope}],
                real_model_acceptance="NOT_RUN",
                human_acceptance="NOT_RUN",
            )

    def _recover(self, db):
        rows = db.execute(
            "SELECT id,event FROM operations WHERE status='running'"
        ).fetchall()
        for operation, event in rows:
            scope = db.execute(
                "SELECT scope FROM events WHERE id=?", (event,)
            ).fetchone()[0]
            result = dict(
                operation=operation,
                scope=scope,
                status="unknown",
                error="interrupted",
                effect="none_confirmed",
            )
            db.execute(
                "UPDATE operations SET status='unknown',result=? WHERE id=?",
                (_json(result), operation),
            )
        return len(rows)

    def recover(self):
        # The mutex cannot be acquired while an existing step is still executing.
        with self._exclusive(), self._open() as db, db:
            return self._recover(db)

    def _validate(self, db, value, frame):
        if type(value) is not dict or set(value) != {
            "action",
            "text",
            "reason",
            "based_on",
            "question_key",
            "updates",
        }:
            raise LabError("invalid_decision")
        if value["action"] not in ACTIONS:
            raise LabError("invalid_action")
        _text(value["text"], empty=value["action"] == "no_action")
        _text(value["reason"], 800)
        sources = {e["id"] for e in frame["sources"]}

        def refs(items):
            if (
                type(items) is not list
                or not 1 <= len(items) <= 8
                or any(type(s) is not str for s in items)
                or not set(items) <= sources
            ):
                raise LabError("invalid_sources")

        refs(value["based_on"])
        changes = value["updates"]
        if type(changes) is not list or len(changes) > 4:
            raise LabError("invalid_updates")
        scope = frame["scope"]
        heads = {s["key"]: s for s in frame["memory"] if s["scope"] == scope}
        seen = set()
        for change in changes:
            if type(change) is not dict or set(change) != {
                "key",
                "kind",
                "text",
                "reason",
                "sources",
                "base_revision",
            }:
                raise LabError("invalid_update")
            key = _id(change["key"])
            if key in seen or change["kind"] not in STATE_KINDS:
                raise LabError("invalid_update")
            seen.add(key)
            _text(change["text"], 800)
            _text(change["reason"], 800)
            refs(change["sources"])
            old = heads.get(key)
            expected = 0 if old is None else old["revision"]
            if (
                type(change["base_revision"]) is not int
                or change["base_revision"] != expected
            ):
                raise LabError("revision_conflict")
            heads[key] = dict(change, scope=scope, revision=expected + 1)
        if len(heads) + sum(s["scope"] != scope for s in frame["memory"]) > MAX_STATES:
            raise LabError("memory_full")
        if value["action"] == "research":
            key = value["question_key"]
            if (
                key not in heads
                or heads[key]["kind"] != "question"
                or scope != "public"
            ):
                raise LabError("invalid_research_question")
            for row in db.execute(
                "SELECT id,result FROM operations WHERE status='decided'"
            ):
                previous = json.loads(row["result"])
                if (
                    previous["scope"] == scope
                    and previous["decision"]["question_key"] == key
                ):
                    completed = db.execute(
                        "SELECT 1 FROM events WHERE kind='research' AND parent=?",
                        (row["id"],),
                    ).fetchone()
                    if completed is None:
                        raise LabError("research_pending")
        elif value["question_key"] is not None:
            raise LabError("unexpected_question")
        return [heads[c["key"]] for c in changes]

    def step(self, operation, event, model):
        _id(operation)
        _id(event)
        started = monotonic()
        with self._exclusive():
            with self._open() as db, db:
                self._recover(db)
                existing = db.execute(
                    "SELECT event,result FROM operations WHERE id=?", (operation,)
                ).fetchone()
                if existing is not None:
                    if existing["event"] != event:
                        return dict(status="failed", error="operation_conflict")
                    return json.loads(existing["result"])
                trigger = db.execute(
                    "SELECT scope FROM events WHERE id=?", (event,)
                ).fetchone()
                if trigger is None:
                    raise LabError("event_missing")
                scope = trigger[0]
                model_kind = _id(getattr(model, "kind", "recorded"))
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?,?,'running',NULL,NULL)",
                    (operation, event, scope, model_kind),
                )
            result = dict(
                operation=operation,
                scope=scope,
                model_kind=model_kind,
                effect="none_confirmed",
            )
            context_done = model_done = started
            try:
                with self._open() as db:
                    maximum = getattr(model, "max_calls", None)
                    if (
                        maximum is not None
                        and db.execute(
                            "SELECT COUNT(*) FROM operations WHERE model_kind=?",
                            (model_kind,),
                        ).fetchone()[0]
                        > maximum
                    ):
                        raise ModelFailure("operation_limit")
                    frame = self._context(db, event)
                with self._open() as db, db:
                    db.execute(
                        "UPDATE operations SET frame=? WHERE id=?",
                        (_json(frame), operation),
                    )
                context_done = monotonic()
                try:
                    value = model.complete(frame)
                finally:
                    model_done = monotonic()
                with self._open() as db, db:
                    updates = self._validate(db, value, frame)
                    for item in updates:
                        db.execute(
                            "INSERT INTO revisions VALUES (?,?,?,?,?)",
                            (
                                scope,
                                item["key"],
                                item["revision"],
                                _json(item),
                                operation,
                            ),
                        )
                    candidate = result | dict(
                        status="decided", decision=value, effect="shadow_only"
                    )
                    db.execute(
                        "UPDATE operations SET status='decided',result=? WHERE id=?",
                        (_json(candidate), operation),
                    )
                    # Include both the proposed memory and this decision in the
                    # next projection. A failure rolls back both atomically.
                    self._context(db, event)
                    result = candidate
            except Exception as error:
                code = str(error) if isinstance(error, LabError) else "provider_error"
                allowed = {
                    "incomplete",
                    "timeout",
                    "provider_error",
                    "budget_blocked",
                    "stopped",
                    "context_full",
                    "memory_full",
                    "research_pending",
                    "revision_conflict",
                    "invalid_sources",
                    "invalid_decision",
                    "invalid_action",
                    "invalid_text",
                    "invalid_updates",
                    "invalid_update",
                    "invalid_id",
                    "invalid_research_question",
                    "unexpected_question",
                    "operation_limit",
                    "refused",
                    "invalid_output",
                }
                result.update(
                    status="failed",
                    error=code if code in allowed else "invalid_decision",
                )
            finished = monotonic()
            result["timing_ms"] = dict(
                context=round((context_done - started) * 1000, 3),
                model=round((model_done - context_done) * 1000, 3),
                total=round((finished - started) * 1000, 3),
            )
            result["observed_at"] = datetime.now(UTC).isoformat()
            with self._open() as db, db:
                db.execute(
                    "UPDATE operations SET status=?,result=? WHERE id=?",
                    (result["status"], _json(result), operation),
                )
            return result
