"""Explicit finite publication authority; model text cannot issue or renew it."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json

from app.integration.threads_stage0 import MAX_INTENTS, _binding, _identity
from app.threads.domain import (
    ApprovalPurpose,
    OutboxState,
    PublicationType,
    issue_approval,
    require_aware_time,
    require_reference,
)
from app.threads.service import PolicyDeniedError
from app.threads.store import ThreadsStoreError


@dataclass(frozen=True, slots=True)
class PublicGrantSpec:
    grant_id: str
    account_ref: str
    window_id: str
    expires_at: datetime
    max_publications: int
    max_http_requests: int
    publication_types: tuple[str, ...]

    def __post_init__(self):
        for value in (self.grant_id, self.account_ref, self.window_id):
            require_reference("public grant binding", value)
        require_aware_time("public grant expiry", self.expires_at)
        for value, maximum in (
            (self.max_publications, MAX_INTENTS),
            (self.max_http_requests, 10 * MAX_INTENTS),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid finite public grant limit")
        if (
            type(self.publication_types) is not tuple
            or not self.publication_types
            or any(
                type(v) is not str or v not in ("post", "reply")
                for v in self.publication_types
            )
            or len(set(self.publication_types)) != len(self.publication_types)
        ):
            raise ValueError("invalid public grant publication types")

    def encoded(self):
        return json.dumps(
            asdict(self) | {"expires_at": self.expires_at.astimezone(UTC).isoformat()},
            sort_keys=True,
        )


class PublicPublicationGrants:
    def __init__(self, app):
        self.app = app

    def _enabled(self):
        if not self.app.autonomous_publication_enabled:
            raise PolicyDeniedError("autonomous publication disabled")
        self.app.publication_guard()

    def _controls(self):
        app = self.app
        policy = app.policy
        if app.mission is None:
            raise PolicyDeniedError("public grant mission unavailable")
        root = (
            app.mission._root_snapshot()[1]
            if app.mission_evolution is not None
            else app.mission.snapshot()[1]
        )
        profile = app.current_research_profile()
        if app.research_enabled and profile is None:
            raise PolicyDeniedError("public grant research authority unavailable")
        return json.dumps(
            {
                "account": app.account,
                "provider_user": app.simulator.provider_user_id,
                "generation": app.ledger.control_generation(),
                "root": root,
                "scopes": sorted(policy.scopes),
                "stage": policy.stage.value,
                "research": None if profile is None else profile.digest,
                "worker": (str(app.model.socket_path), app.model.expected_uid),
                "free_missions": app.free_missions_enabled,
            },
            sort_keys=True,
        )

    def _schema(self, db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS public_grants("
            "id TEXT PRIMARY KEY,spec TEXT NOT NULL,window TEXT NOT NULL,"
            "controls TEXT NOT NULL,issuer TEXT NOT NULL,revoked INTEGER NOT NULL,"
            "last_seen TEXT NOT NULL,http_used INTEGER NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS public_grant_admissions("
            "operation TEXT PRIMARY KEY,grant_id TEXT NOT NULL,binding TEXT NOT NULL,"
            "reference TEXT NOT NULL,phase TEXT NOT NULL)"
        )

    def _audit(self, db, operation, action, actor, reference):
        db.execute(
            "INSERT INTO public_audit(operation,action,actor,at,reference) VALUES(?,?,?,?,?)",
            (operation, action, actor, self.app.clock.now().isoformat(), reference),
        )

    def issue(self, spec, window):
        self._enabled()
        actor = self.app.operator.authenticate()
        now = self.app.clock.now().astimezone(UTC)
        if (
            type(spec) is not PublicGrantSpec
            or self.app.mission is None
            or spec.account_ref != self.app.account
            or window.window_id != spec.window_id
            or window.publication_grant != spec.grant_id
            or not now < spec.expires_at <= window.expires_at
            or window.expires_at - now > timedelta(days=1)
        ):
            raise ValueError("public grant does not match a finite reviewed window")
        for kind in spec.publication_types:
            if self.app.policy.publication_blockers(
                PublicationType(kind), live_transport_enabled=True
            ):
                raise PolicyDeniedError("public grant publication policy denies scope")
        controls = self._controls()
        with self.app.journal.transaction() as db:
            self._schema(db)
            if db.execute(
                "SELECT 1 FROM public_grants WHERE id=?", (spec.grant_id,)
            ).fetchone():
                raise ValueError("public grant already issued")
            if (
                db.execute("SELECT COUNT(*) FROM public_grants").fetchone()[0]
                >= MAX_INTENTS
            ):
                raise ValueError("public grant capacity reached")
            db.execute(
                "INSERT INTO public_grants VALUES(?,?,?,?,?,0,?,0)",
                (
                    spec.grant_id,
                    spec.encoded(),
                    window.encoded(),
                    controls,
                    actor,
                    now.isoformat(),
                ),
            )
            self._audit(db, spec.grant_id, "grant_issue", actor, spec.grant_id)
        return self.summary(spec.grant_id)

    def revoke(self, grant_id):
        actor = self.app.operator.authenticate()
        with self.app.journal.transaction() as db:
            self._schema(db)
            if (
                db.execute(
                    "UPDATE public_grants SET revoked=1 WHERE id=?", (grant_id,)
                ).rowcount
                != 1
            ):
                raise ValueError("public grant missing")
            self._audit(db, grant_id, "grant_revoke", actor, grant_id)

    def require_spec(self, spec):
        with self.app.journal.transaction() as db:
            self._schema(db)
            row = db.execute(
                "SELECT spec FROM public_grants WHERE id=?", (spec.grant_id,)
            ).fetchone()
        if row is None or row[0] != spec.encoded():
            raise PolicyDeniedError(
                "public grant configuration differs from issued authority"
            )

    def _denial_in(self, db, grant_id):
        row = db.execute(
            "SELECT * FROM public_grants WHERE id=?", (grant_id,)
        ).fetchone()
        if row is None:
            return "public grant missing", None
        now = self.app.clock.now().astimezone(UTC)
        observed = max(now, datetime.fromisoformat(row["last_seen"]))
        db.execute(
            "UPDATE public_grants SET last_seen=? WHERE id=?",
            (observed.isoformat(), grant_id),
        )
        spec = json.loads(row["spec"])
        window = self.app.operating_window
        try:
            controls = self._controls()
        except (OSError, ValueError, PolicyDeniedError):
            controls = None
        if row["controls"] != controls:
            db.execute("UPDATE public_grants SET revoked=1 WHERE id=?", (grant_id,))
            return "public grant controls changed", row
        if (
            row["revoked"]
            or observed >= datetime.fromisoformat(spec["expires_at"])
            or now < datetime.fromisoformat(row["last_seen"])
            or window is None
            or window.spec.encoded() != row["window"]
        ):
            return "public grant revoked, expired or controls changed", row
        return None, row

    def current_in(self, db, item, reference):
        if not self.app.autonomous_publication_enabled:
            return None
        admission = db.execute(
            "SELECT * FROM public_grant_admissions WHERE operation=? AND reference=?",
            (item.operation_id, reference),
        ).fetchone()
        if admission is None:
            return None
        stored = db.execute(
            "SELECT * FROM approvals WHERE reference=? AND operation=?",
            (reference, item.operation_id),
        ).fetchone()
        if (
            stored is None
            or stored["revoked"]
            or stored["binding"] != _binding(item)
            or stored["purpose"] != ApprovalPurpose.AUTONOMOUS_PUBLICATION.value
        ):
            return None
        denial, row = self._denial_in(db, admission["grant_id"])
        if (
            denial is not None
            or admission["binding"] != _binding(item)
            or admission["phase"] not in ("reserved", "attempted")
        ):
            return None
        return issue_approval(
            reference=reference,
            account_ref=self.app.account,
            operation_id=item.operation_id,
            expires_at=datetime.fromisoformat(json.loads(row["spec"])["expires_at"]),
            purpose=ApprovalPurpose.AUTONOMOUS_PUBLICATION,
        )

    def activate_window(self, window):
        with self.app.journal.transaction() as db:
            self._schema(db)
            denial, _ = self._denial_in(db, window.spec.publication_grant)
        if denial is not None:
            raise PolicyDeniedError(denial)
        self._enabled()

    def _admit(self, grant_id, item):
        self._enabled()
        self.app._public_recovery_item(item.operation_id)
        denial = None
        approval = None
        with self.app.journal.transaction() as db:
            denial, row = self._denial_in(db, grant_id)
            if denial is None:
                spec = json.loads(row["spec"])
                origin = db.execute(
                    "SELECT 1 FROM public_window_work WHERE window=? AND operation=? "
                    "AND kind IN ('wake','reply','mention')",
                    (spec["window_id"], item.operation_id),
                ).fetchone()
                if (
                    origin is None
                    or item.publication_type.value not in spec["publication_types"]
                    or item.replacement_of is not None
                ):
                    denial = "public grant draft outside scope"
                else:
                    prior = db.execute(
                        "SELECT * FROM public_grant_admissions WHERE operation=?",
                        (item.operation_id,),
                    ).fetchone()
                    if prior is not None:
                        if (
                            prior["grant_id"] == grant_id
                            and prior["phase"] == "reserved"
                        ):
                            approval = self.current_in(db, item, prior["reference"])
                    elif item.approval_reference is None:
                        used = db.execute(
                            "SELECT COUNT(*) FROM public_grant_admissions WHERE grant_id=?",
                            (grant_id,),
                        ).fetchone()[0]
                        if used >= spec["max_publications"]:
                            denial = "public grant publications exhausted"
                        elif (
                            db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
                            >= MAX_INTENTS
                        ):
                            denial = "public grant approval capacity reached"
                        else:
                            reference = _identity(
                                "public-grant",
                                grant_id,
                                item.operation_id,
                                _binding(item),
                            )
                            db.execute(
                                "INSERT INTO public_grant_admissions VALUES(?,?,?,?,'reserved')",
                                (
                                    item.operation_id,
                                    grant_id,
                                    _binding(item),
                                    reference,
                                ),
                            )
                            db.execute(
                                "INSERT INTO approvals VALUES(?,?,?,?,0,?,?)",
                                (
                                    reference,
                                    item.operation_id,
                                    _binding(item),
                                    spec["expires_at"],
                                    ApprovalPurpose.AUTONOMOUS_PUBLICATION.value,
                                    "grant:" + grant_id,
                                ),
                            )
                            self._audit(
                                db,
                                item.operation_id,
                                "grant_admit",
                                "grant:" + grant_id,
                                reference,
                            )
                            approval = self.current_in(db, item, reference)
        if denial is not None:
            raise PolicyDeniedError(denial)
        return approval

    def _reserve_http(self, grant_id, item, approval):
        self._enabled()
        self.app.service._check_ready_policy(item, approval, now=self.app.clock.now())
        denial = None
        with self.app.journal.transaction() as db:
            denial, row = self._denial_in(db, grant_id)
            if denial is None:
                admission = db.execute(
                    "SELECT phase FROM public_grant_admissions WHERE operation=? AND grant_id=?",
                    (item.operation_id, grant_id),
                ).fetchone()
                spec = json.loads(row["spec"])
                if admission is None or admission[0] != "attempted":
                    denial = "public grant dispatch admission unavailable"
                elif row["http_used"] >= spec["max_http_requests"]:
                    denial = "public grant HTTP requests exhausted"
                else:
                    db.execute(
                        "UPDATE public_grants SET http_used=http_used+1 WHERE id=?",
                        (grant_id,),
                    )
                    self._audit(
                        db,
                        item.operation_id,
                        "grant_http_reserve",
                        "grant:" + grant_id,
                        approval.reference,
                    )
        if denial is not None:
            raise PolicyDeniedError(denial)

    def dispatch(self, grant_id, operation):
        item = self.app.service.store.get(operation)
        if item.state not in (OutboxState.AWAITING_APPROVAL, OutboxState.READY):
            return None
        approval = self._admit(grant_id, item)
        if approval is None:
            return None
        if item.state is OutboxState.AWAITING_APPROVAL:
            self.app.service.authorize(operation, approval, now=self.app.clock.now())
        with self.app.journal.transaction() as db:
            changed = db.execute(
                "UPDATE public_grant_admissions SET phase='attempted' "
                "WHERE operation=? AND grant_id=? AND phase='reserved'",
                (operation, grant_id),
            ).rowcount
        if changed != 1:
            return None
        with self.app.simulator.bounded_requests(
            lambda: self._reserve_http(grant_id, item, approval)
        ):
            result = self.app._dispatch_approved(operation, approval)
        with self.app.journal.transaction() as db:
            db.execute(
                "UPDATE public_grant_admissions SET phase='finished' WHERE operation=?",
                (operation,),
            )
        return result

    def drain(self, window):
        self._enabled()
        if self.app.service.store.unresolved_for_account(
            self.app.account
        ) or self.app.service.store.has_sending_for_account(self.app.account):
            raise PolicyDeniedError(
                "autonomous account paused by unresolved publication"
            )
        for operation in window.operations():
            try:
                self.app.service.store.get(operation)
            except ThreadsStoreError:
                continue
            self.dispatch(window.spec.publication_grant, operation)

    def summary(self, grant_id):
        with self.app.journal.transaction() as db:
            row = db.execute(
                "SELECT * FROM public_grants WHERE id=?", (grant_id,)
            ).fetchone()
            if row is None:
                raise ValueError("public grant missing")
            count = db.execute(
                "SELECT COUNT(*) FROM public_grant_admissions WHERE grant_id=?",
                (grant_id,),
            ).fetchone()[0]
        return {
            "grant_id": grant_id,
            "spec": json.loads(row["spec"]),
            "revoked": bool(row["revoked"]),
            "issuer": row["issuer"],
            "publications_reserved": count,
            "http_requests_reserved": row["http_used"],
            "last_seen": row["last_seen"],
        }
