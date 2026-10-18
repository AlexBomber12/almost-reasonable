"""Explicit synthetic participant control composition for the private web boundary."""

from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from html import escape
from urllib.parse import quote

from app.core.store import EventStore
from app.privacy.application import (
    EventStoreErasureAdapter,
    PrivacyReadBlocked,
    PrivacyReadGate,
)
from app.privacy.consent import ConsentService
from app.privacy.deletion import DeletionCoordinator
from app.privacy.ledger import PrivacyLedger, _time
from app.privacy.models import (
    ConsentGrant,
    ConsentPurpose,
    ContentCategory,
    DeletionDisposition,
    DeletionRequest,
    NOTICE_VERSION,
    OperationOrigin,
    TrustedPrivacyContext,
    require_token,
)
from app.privacy.notice import (
    NARRATIVE_DISCLOSURE_RU,
    NARRATIVE_DISCLOSURE_VERSION,
    NOTICE_RU,
    NoticeService,
)

from .auth import AuthStore, AuthenticatedSession


class PrivacyControlError(ValueError):
    def __init__(self, status: int):
        self.status = status


class ParticipantPrivacyControls:
    """Enabled only by trusted bootstrap with matching authentication and stores."""

    def __init__(
        self,
        auth: AuthStore,
        store: EventStore,
        ledger: PrivacyLedger,
        gate: PrivacyReadGate,
        *,
        clock: Callable[[], datetime],
    ):
        if gate._store is not store or gate._ledger is not ledger:
            raise ValueError("privacy dependencies must match")
        self.auth = auth
        self.store = store
        self.ledger = ledger
        self.gate = gate
        self.clock = clock
        self.notice = NoticeService(ledger, clock=clock)
        self.consent = ConsentService(ledger, clock=clock)
        self.deletion = DeletionCoordinator(
            ledger, EventStoreErasureAdapter(store), clock=clock
        )

    @staticmethod
    def context(session: AuthenticatedSession) -> TrustedPrivacyContext:
        return TrustedPrivacyContext(
            session.principal_id.value,
            session.authenticated_source.value,
            session.authenticated_source.value,
            OperationOrigin.CONTROL,
        )

    def acknowledged(self, session: AuthenticatedSession) -> bool:
        return self.notice.has_acknowledged(session.principal_id.value, NOTICE_VERSION)

    def source(self, session: AuthenticatedSession, reference: str) -> str:
        try:
            require_token(reference, "source")
        except ValueError:
            raise PrivacyControlError(400) from None

        return self.gate.authorized(lambda: self._source_unfenced(session, reference))

    def _source_unfenced(self, session: AuthenticatedSession, reference: str) -> str:
        with closing(self.store._open()) as db:
            self.store._verify_current_schema(db)
            row = db.execute(
                "SELECT p.content FROM event_envelopes e "
                "JOIN private_payloads p ON p.payload_id=e.payload_id "
                "WHERE e.event_id=? AND e.audience_principal_id=? "
                "AND e.event_type='participant_input' AND e.channel='private_chat' "
                "AND p.audience_principal_id=e.audience_principal_id "
                "AND p.retention_status='active'",
                (reference, session.principal_id.value),
            ).fetchone()
            if (
                row is None
                or type(row[0]) is not str
                or not 1 <= len(row[0].encode()) <= 8192
            ):
                raise PrivacyControlError(404)
            return row[0]

    def page(
        self,
        session: AuthenticatedSession,
        csrf: str,
        before: str | None = None,
        source_before: str | None = None,
        deletion_before: str | None = None,
    ) -> bytes:
        acknowledged = self.acknowledged(session)
        status = "Подтверждено" if acknowledged else "Требуется подтверждение"
        with closing(self.ledger._open()) as db:
            cursor = None
            if before is not None:
                self._token(before)
                cursor = db.execute(
                    "SELECT granted_at,consent_id FROM consent_grants "
                    "WHERE principal_id=? AND consent_id=?",
                    (session.principal_id.value, before),
                ).fetchone()
                if cursor is None:
                    raise PrivacyControlError(404)
            query = (
                "SELECT g.consent_id,g.content_reference,g.audience,g.purpose,g.expires_at,"
                "r.revoked_at,EXISTS(SELECT 1 FROM deletion_records d "
                "WHERE d.principal_id=g.principal_id AND d.content_reference=g.content_reference "
                "AND d.disposition IN ('pending','applied','incomplete_blocked')) "
                "FROM consent_grants g LEFT JOIN consent_revocations r "
                "ON r.consent_id=g.consent_id WHERE g.principal_id=? "
            )
            params: tuple[str, ...] = (session.principal_id.value,)
            if cursor is not None:
                query += "AND (g.granted_at<? OR (g.granted_at=? AND g.consent_id<?)) "
                params += (cursor[0], cursor[0], cursor[1])
            query += "ORDER BY g.granted_at DESC,g.consent_id DESC LIMIT 21"
            grants = db.execute(query, params).fetchall()
        has_more = len(grants) > 20
        grants = grants[:20]
        next_link = (
            f'<a href="/privacy?before={quote(grants[-1][0], safe="")}">'
            "Следующие согласия</a>"
            if has_more
            else ""
        )
        now = self.clock()
        audience_names = {
            "public": "Публичная аудитория",
        }
        purpose_names = {
            "identifiable_quotation": "Идентифицируемая цитата",
            "identifiable_disclosure": "Идентифицируемое раскрытие",
        }
        rows = "".join(
            "<li>"
            + " · ".join(
                escape(str(value))
                for value in (
                    row[0],
                    row[1],
                    audience_names.get(row[2], row[2]),
                    purpose_names.get(row[3], row[3]),
                    row[4],
                )
            )
            + (
                " · Использование заблокировано удалением"
                if row[6]
                else " · Отозвано"
                if row[5]
                else " · Срок истёк"
                if _time(row[4]) <= now
                else " · Действует до срока"
            )
            + (
                '<form method="post" action="/api/privacy/revocations" data-endpoint="/api/privacy/revocations">'
                f'<input type="hidden" name="consent_id" value="{escape(row[0])}">'
                "<button>Отозвать</button></form>"
                if row[5] is None
                else ""
            )
            + "</li>"
            for row in grants
        )
        deletion_labels = {
            "pending": "Ожидает удаления",
            "applied": "Локальное удаление завершено",
            "incomplete_blocked": "Удаление не завершено",
        }
        deletion_status = self.status(session, deletion_before)
        deletion_rows = "".join(
            f"<li>{escape(row['operation_id'])}: "
            f"{deletion_labels.get(row['status'], 'Операция отклонена')}</li>"
            for row in deletion_status["deletions"]
        )
        deletion_next_link = (
            f'<a href="/privacy?deletion_before={quote(deletion_status["next_cursor"], safe="")}">'
            "Следующие удаления</a>"
            if deletion_status["next_cursor"] is not None
            else ""
        )
        try:
            if source_before is not None:
                self._token(source_before)
            source_rows, source_has_more = self.gate.authorized(
                lambda: self._source_ids(session, source_before)
            )
        except PrivacyReadBlocked:
            source_rows = ()
            source_has_more = False
        source_links = "".join(
            f'<li><a href="/privacy/source?ref={quote(ref, safe="")}">'
            f"Сообщение {index}</a></li>"
            for index, ref in enumerate(source_rows, 1)
        )
        source_next_link = (
            f'<a href="/privacy?source_before={quote(source_rows[-1], safe="")}">'
            "Следующие источники</a>"
            if source_has_more
            else ""
        )
        return (
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            "<title>Приватность</title><h1>Приватность</h1>"
            '<nav><a href="/chat">Чат</a></nav>'
            f"<p>Версия {escape(NOTICE_VERSION)}: {status}</p>"
            f"<pre>{escape(NOTICE_RU)}</pre>"
            '<section id="narrative-disclosure" '
            f'data-version="{NARRATIVE_DISCLOSURE_VERSION}">'
            f"<p>{escape(NARRATIVE_DISCLOSURE_RU)}</p></section>"
            f'<main data-csrf="{escape(csrf)}">'
            '<form method="post" action="/api/privacy/notice" data-endpoint="/api/privacy/notice"><h2>Подтверждение уведомления</h2>'
            f'<input type="hidden" name="version" value="{escape(NOTICE_VERSION)}">'
            "<button>Подтвердить уведомление</button></form>"
            "<p>Отдельное согласие необязательно для приватного разговора.</p>"
            "<h2>Мои источники</h2><ol>"
            + source_links
            + "</ol>"
            + source_next_link
            + "<h2>Согласия</h2><ol>"
            + rows
            + "</ol>"
            + next_link
            + "<h2>Статус локального удаления</h2><ol>"
            + deletion_rows
            + "</ol>"
            + deletion_next_link
            + '<form method="post" action="/api/privacy/reconcile" data-endpoint="/api/privacy/reconcile"><h2>Повторить локальное удаление</h2>'
            "<button>Проверить незавершённые операции</button></form>"
            '<button id="privacy-logout">Выйти</button>'
            '</main><script src="/assets/privacy.js"></script></html>'
        ).encode()

    def _source_ids(
        self, session: AuthenticatedSession, before: str | None = None
    ) -> tuple[tuple[str, ...], bool]:
        with closing(self.store._open()) as db:
            self.store._verify_current_schema(db)
            cursor = None
            if before is not None:
                cursor = db.execute(
                    "SELECT e.occurred_at,e.event_id FROM event_envelopes e "
                    "JOIN private_payloads p ON p.payload_id=e.payload_id "
                    "WHERE e.event_id=? AND e.audience_principal_id=? "
                    "AND e.event_type='participant_input' AND e.channel='private_chat' "
                    "AND p.retention_status='active'",
                    (before, session.principal_id.value),
                ).fetchone()
                if cursor is None:
                    raise PrivacyControlError(404)
            query = (
                "SELECT e.event_id FROM event_envelopes e "
                "JOIN private_payloads p ON p.payload_id=e.payload_id "
                "WHERE e.audience_principal_id=? AND e.event_type='participant_input' "
                "AND e.channel='private_chat' AND p.retention_status='active' "
            )
            params: tuple[str, ...] = (session.principal_id.value,)
            if cursor is not None:
                query += "AND (e.occurred_at<? OR (e.occurred_at=? AND e.event_id<?)) "
                params += (cursor[0], cursor[0], cursor[1])
            query += "ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT 21"
            rows = db.execute(query, params).fetchall()
        return tuple(row[0] for row in rows[:20]), len(rows) > 20

    def source_page(
        self, session: AuthenticatedSession, csrf: str, reference: str
    ) -> bytes:
        content = self.source(session, reference)
        ref = escape(reference)
        return (
            '<!doctype html><html lang="ru"><meta charset="utf-8">'
            "<title>Источник</title><h1>Управление источником</h1>"
            f'<pre>{escape(content)}</pre><main data-csrf="{escape(csrf)}">'
            "<p>Согласие относится только к этому точному сообщению, аудитории, цели и сроку.</p>"
            '<form method="post" action="/api/privacy/grants" data-endpoint="/api/privacy/grants"><h2>Дать согласие</h2>'
            f'<input type="hidden" name="source" value="{ref}">'
            '<label>Аудитория <select name="audience">'
            '<option value="" selected disabled>Выберите аудиторию</option>'
            '<option value="public">Публичная аудитория</option></select></label>'
            '<label>Цель <select name="purpose">'
            '<option value="" selected disabled>Выберите цель</option>'
            '<option value="identifiable_quotation">Идентифицируемая цитата</option>'
            '<option value="identifiable_disclosure">Идентифицируемое раскрытие</option>'
            '</select></label><label>Срок <select name="validity_hours">'
            '<option value="" selected disabled>Выберите срок</option>'
            '<option value="1">1 час</option><option value="24">24 часа</option>'
            '<option value="168">7 дней</option></select></label>'
            "<button>Дать отдельное согласие</button></form>"
            "<p>Удаление этого источника инвалидирует зависимые локальные памяти и ответы. "
            "Оно не удаляет данные у внешних обработчиков и уже опубликованный материал.</p>"
            '<form method="post" action="/api/privacy/deletions" data-endpoint="/api/privacy/deletions"><h2>Удалить источник</h2>'
            f'<input type="hidden" name="source" value="{ref}">'
            '<label><input type="checkbox" name="confirm" value="delete" required>'
            "Подтверждаю локальное удаление</label><button>Удалить</button></form>"
            '</main><script src="/assets/privacy.js"></script></html>'
        ).encode()

    @staticmethod
    def _current(check: Callable[[], bool]) -> None:
        if not check():
            raise PrivacyControlError(401)

    def acknowledge(
        self,
        session: AuthenticatedSession,
        version: str,
        current_authorized: Callable[[], bool],
    ) -> dict[str, str]:
        if type(version) is not str:
            raise PrivacyControlError(400)
        context = self.context(session)
        try:
            result = self.notice.acknowledge(
                context,
                version,
                self.clock(),
                "notice:" + session.principal_id.value,
                authorized_check=lambda: self._current(current_authorized),
            )
        except PrivacyControlError:
            raise
        except ValueError:
            raise PrivacyControlError(400) from None
        return {"status": result.reason}

    def grant(
        self,
        session: AuthenticatedSession,
        data: dict[str, object],
        current_authorized: Callable[[], bool],
    ) -> tuple[int, dict[str, str]]:
        op = self._operation_token(data["operation_id"])
        source = self._token(data["source"])
        audience = data["audience"]
        purpose = data["purpose"]
        hours = data["validity_hours"]
        if (
            type(audience) is not str
            or type(purpose) is not str
            or audience != "public"
            or purpose not in {p.value for p in ConsentPurpose}
            or type(hours) is not int
            or hours not in {1, 24, 168}
        ):
            raise PrivacyControlError(400)
        with closing(self.ledger._open()) as db:
            prior = db.execute(
                "SELECT consent_id,principal_id,content_reference,audience,purpose,granted_at,expires_at "
                "FROM consent_grants WHERE operation_id=? OR consent_id=?",
                (op, "consent:" + op),
            ).fetchone()
        if prior is not None:
            if prior[1] != session.principal_id.value:
                raise PrivacyControlError(404)
            if prior[1:5] != (
                session.principal_id.value,
                source,
                audience,
                purpose,
            ) or _time(prior[6]) - _time(prior[5]) != timedelta(hours=hours):
                raise PrivacyControlError(409)
            return 200, {"status": "already_granted", "consent_id": prior[0]}
        context = self.context(session)
        now = self.clock()
        with closing(self.ledger._open()) as db:
            latest_revision = db.execute(
                "SELECT MAX(revision) FROM consent_grants WHERE principal_id=? "
                "AND content_reference=? AND audience=? AND purpose=?",
                (session.principal_id.value, source, audience, purpose),
            ).fetchone()[0]
        grant = ConsentGrant(
            "consent:" + op,
            op,
            context.principal_id,
            source,
            audience,
            ConsentPurpose(purpose),
            now,
            now + timedelta(hours=hours),
            context.actor_reference,
            1 if latest_revision is None else latest_revision + 1,
            "grant:" + op,
            context.authentication_event_id,
        )
        try:
            self.source(session, source)
            result = self.consent.grant(
                context,
                grant,
                source_check=lambda: self._source_unfenced(session, source),
                authorized_check=lambda: self._current(current_authorized),
            )
        except PrivacyControlError:
            raise
        except ValueError:
            return self._grant_retry(session, op, source, audience, purpose, hours)
        return 201, {"status": result.reason, "consent_id": grant.consent_id}

    def _grant_retry(self, session, op, source, audience, purpose, hours):
        with closing(self.ledger._open()) as db:
            row = db.execute(
                "SELECT consent_id,principal_id,content_reference,audience,purpose,granted_at,expires_at "
                "FROM consent_grants WHERE operation_id=?",
                (op,),
            ).fetchone()
        if row is None:
            raise PrivacyControlError(409)
        if row[1] != session.principal_id.value:
            raise PrivacyControlError(404)
        if row[2:5] != (source, audience, purpose) or _time(row[6]) - _time(
            row[5]
        ) != timedelta(hours=hours):
            raise PrivacyControlError(409)
        return 200, {"status": "already_granted", "consent_id": row[0]}

    def revoke(
        self,
        session: AuthenticatedSession,
        data: dict[str, object],
        current_authorized: Callable[[], bool],
    ) -> tuple[int, dict[str, str]]:
        op = self._operation_token(data["operation_id"])
        consent_id = self._token(data["consent_id"])
        with closing(self.ledger._open()) as db:
            prior = db.execute(
                "SELECT consent_id,principal_id FROM consent_revocations WHERE operation_id=?",
                (op,),
            ).fetchone()
        if prior is not None:
            if prior[1] != session.principal_id.value:
                raise PrivacyControlError(404)
            if prior != (consent_id, session.principal_id.value):
                raise PrivacyControlError(409)
            return 200, {"status": "already_revoked"}
        context = self.context(session)
        try:
            result = self.consent.revoke(
                context,
                consent_id=consent_id,
                operation_id=op,
                revoked_at=self.clock(),
                correlation_id="revoke:" + op,
                authorized_check=lambda: self._current(current_authorized),
            )
        except PrivacyControlError:
            raise
        except ValueError:
            with closing(self.ledger._open()) as db:
                row = db.execute(
                    "SELECT consent_id,principal_id FROM consent_revocations WHERE operation_id=?",
                    (op,),
                ).fetchone()
            if row == (consent_id, session.principal_id.value):
                return 200, {"status": "already_revoked"}
            raise PrivacyControlError(409) from None
        if result.reason == "invalid_authorization":
            raise PrivacyControlError(404)
        self.gate.reconcile()
        return 201, {"status": result.reason}

    def delete(
        self,
        session: AuthenticatedSession,
        data: dict[str, object],
        current_authorized: Callable[[], bool],
    ) -> dict[str, str]:
        op = self._operation_token(data["operation_id"])
        source = self._token(data["source"])
        if data["confirm"] != "delete":
            raise PrivacyControlError(400)
        try:
            prior = self.ledger.deletion(op)
        except KeyError:
            prior = None
        if prior is not None:
            if prior.request.principal_id != session.principal_id.value:
                raise PrivacyControlError(404)
            if prior.content_reference != source:
                raise PrivacyControlError(409)
            return self._deletion_result(op, prior.disposition)
        context = self.context(session)
        self.source(session, source)
        request = DeletionRequest(
            op,
            context.principal_id,
            source,
            ContentCategory.PRIVATE_PAYLOAD,
            self.clock(),
            context.actor_reference,
            "delete:" + op,
            context.authentication_event_id,
        )
        try:
            result = self.deletion.delete(
                context,
                request,
                source_check=lambda: self._source_unfenced(session, source),
                authorized_check=lambda: self._current(current_authorized),
            )
        except PrivacyControlError:
            raise
        except ValueError:
            try:
                record = self.ledger.deletion(op)
            except KeyError:
                raise PrivacyControlError(409) from None
            if record.request.principal_id != session.principal_id.value:
                raise PrivacyControlError(404) from None
            if record.content_reference != source:
                raise PrivacyControlError(409) from None
            return self._deletion_result(op, record.disposition)
        return self._deletion_result(op, result.disposition)

    def _deletion_result(
        self, operation_id: str, disposition: DeletionDisposition
    ) -> dict[str, str]:
        if disposition is DeletionDisposition.APPLIED:
            self.gate.reconcile()
        return {"status": disposition.value, "operation_id": operation_id}

    def status(
        self, session: AuthenticatedSession, before: str | None = None
    ) -> dict[str, object]:
        with closing(self.ledger._open()) as db:
            cursor = None
            if before is not None:
                self._token(before)
                cursor = db.execute(
                    "SELECT requested_at,operation_id FROM deletion_records "
                    "WHERE principal_id=? AND operation_id=?",
                    (session.principal_id.value, before),
                ).fetchone()
                if cursor is None:
                    raise PrivacyControlError(404)
            query = (
                "SELECT operation_id,disposition,reason FROM deletion_records "
                "WHERE principal_id=? "
            )
            params: tuple[str, ...] = (session.principal_id.value,)
            if cursor is not None:
                query += "AND (requested_at<? OR (requested_at=? AND operation_id<?)) "
                params += (cursor[0], cursor[0], cursor[1])
            query += "ORDER BY requested_at DESC,operation_id DESC LIMIT 21"
            rows = db.execute(query, params).fetchall()
        has_more = len(rows) > 20
        rows = rows[:20]
        return {
            "deletions": [
                dict(operation_id=row[0], status=row[1], reason=row[2]) for row in rows
            ],
            "next_cursor": rows[-1][0] if has_more else None,
        }

    @staticmethod
    def _token(value: object) -> str:
        try:
            return require_token(value, "operation field")
        except ValueError:
            raise PrivacyControlError(400) from None

    @classmethod
    def _operation_token(cls, value: object) -> str:
        token = cls._token(value)
        if len(token) > 120:
            raise PrivacyControlError(400)
        return token
