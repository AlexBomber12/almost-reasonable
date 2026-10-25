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
    NOTICE_RU,
    NoticeService,
)

from .auth import AuthStore, AuthenticatedSession
from .presentation import back_header, date_label, document


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
                "AND p.audience_principal_id=e.audience_principal_id AND p.retention_status='active'",
                (reference, session.principal_id.value),
            ).fetchone()
            if (
                row is None
                or type(row[0]) is not str
                or not 1 <= len(row[0].encode()) <= 8192
            ):
                raise PrivacyControlError(404)
            return row[0]

    @staticmethod
    def _notice_text() -> str:
        return (
            '<div class="notice-text">'
            + "".join(
                f"<p>{escape(paragraph)}</p>"
                for paragraph in NOTICE_RU.strip().split("\n\n")
            )
            + "</div>"
        )

    @staticmethod
    def _page_document(title: str, content: str) -> bytes:
        return document(
            title, content, script='<script src="/assets/privacy.js" defer></script>'
        )

    def page(
        self,
        session: AuthenticatedSession,
        csrf: str,
        before: str | None = None,
        source_before: str | None = None,
        deletion_before: str | None = None,
    ) -> bytes:
        if not self.acknowledged(session):
            return self._page_document(
                "Прежде чем начать",
                (
                    f'<main class="onboarding" data-csrf="{escape(csrf)}">'
                    '<span class="brand__mark" aria-hidden="true">AR</span>'
                    '<button class="theme-toggle" type="button">Тема</button>'
                    "<h1>Прежде чем начать</h1><ol>"
                    "<li><p><b>Где обрабатываются данные.</b> Текст может передаваться OpenAI; "
                    "трафик туннеля обрабатывает Cloudflare. Владелец и оператор инфраструктуры "
                    "технически могут иметь доступ к локальному хранилищу.</p></li>"
                    "<li><p><b>Ваши слова не публикуются автоматически.</b> Безопасные обобщения могут "
                    "влиять на агента. Для узнаваемой цитаты или раскрытия нужно отдельное точное разрешение.</p></li>"
                    "<li><p><b>Можно запросить удаление.</b> Локальное удаление доступно в настройках "
                    "приватности. У внешних сервисов действуют их правила хранения.</p></li></ol>"
                    "<details><summary>Полный текст уведомления</summary>"
                    + self._notice_text()
                    + "</details>"
                    '<p id="privacy-feedback" role="status" aria-live="polite"></p>'
                    '<form method="post" action="/api/privacy/notice" data-endpoint="/api/privacy/notice">'
                    f'<input type="hidden" name="version" value="{escape(NOTICE_VERSION)}">'
                    "<button>Понятно, к разговору</button></form></main>"
                ),
            )
        with closing(self.ledger._open()) as db:
            acknowledgement = db.execute(
                "SELECT acknowledged_at FROM notice_acknowledgements WHERE principal_id=? AND notice_version=?",
                (session.principal_id.value, NOTICE_VERSION),
            ).fetchone()
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
        next_link = self._next_link("before", grants[-1][0]) if has_more else ""
        now = self.clock()
        if source_before is not None:
            self._token(source_before)
        unavailable = False
        try:
            source_rows, source_has_more, previews = self.gate.authorized(
                lambda: self._page_sources(session, source_before, grants)
            )
        except PrivacyReadBlocked:
            source_rows, source_has_more, previews = (), False, {}
            unavailable = True
        purpose_names = {
            "identifiable_quotation": "Дословно",
            "identifiable_disclosure": "С указанием автора",
        }
        rows = []
        for row in grants:
            label = (
                "Заблокировано удалением"
                if row[6]
                else "Отозвано"
                if row[5]
                else "Истекло"
                if _time(row[4]) <= now
                else "Действует"
            )
            active = label == "Действует"
            preview = previews.get(row[1], "Текст сообщения недоступен")
            revoke = (
                '<form method="post" action="/api/privacy/revocations" data-endpoint="/api/privacy/revocations">'
                f'<input type="hidden" name="consent_id" value="{escape(row[0])}">'
                "<button>Отозвать</button></form>"
                if row[5] is None
                else ""
            )
            rows.append(
                f'<li><span class="preview">{escape(preview[:160])}</span>'
                f'<div class="row-meta"><span>{purpose_names[row[3]]} · Публично, в Threads · '
                f"до {date_label(_time(row[4]))}</span>"
                f'<span class="badge{" badge--ok" if active else ""}">{label}</span>{revoke}</div></li>'
            )
        deletion_status = self.status(session, deletion_before)
        deletion_dates = self._deletion_dates(session, deletion_status["deletions"])
        deletion_rows = []
        labels = {
            "pending": "Ожидает",
            "applied": "Удалено",
            "incomplete_blocked": "Не завершено",
        }
        for row in deletion_status["deletions"]:
            retry = (
                '<form method="post" action="/api/privacy/reconcile" data-endpoint="/api/privacy/reconcile">'
                "<button>Повторить</button></form>"
                if row["status"] in {"pending", "incomplete_blocked"}
                else ""
            )
            deletion_rows.append(
                f"<li><span>Удаление от {date_label(_time(deletion_dates[row['operation_id']]))}</span>"
                f'<div class="row-meta"><span class="badge">{labels.get(row["status"], "Операция отклонена")}</span>'
                f"{retry}</div></li>"
            )
        deletion_next = (
            self._next_link("deletion_before", deletion_status["next_cursor"])
            if deletion_status["next_cursor"]
            else ""
        )
        source_links = "".join(
            f'<li><a class="source-link" href="/privacy/source?ref={quote(ref, safe="")}">'
            f'<span class="preview">{escape(content[:160])}</span>'
            f'<span class="muted">{date_label(_time(occurred))}</span></a></li>'
            for ref, content, occurred in source_rows
        )
        source_next = (
            self._next_link("source_before", source_rows[-1][0])
            if source_has_more
            else ""
        )
        ack_label = (
            "Подтверждено " + date_label(_time(acknowledgement[0]))
            if acknowledgement
            else "Подтверждено"
        )
        blocked = (
            '<p class="feedback">Сообщения временно недоступны: удаление ещё не завершено.</p>'
            if unavailable
            else ""
        )
        deletions = (
            '<section><h2>Удаления</h2><ol class="card-list">'
            + "".join(deletion_rows)
            + "</ol>"
            + deletion_next
            + "</section>"
            if deletion_rows
            else ""
        )
        return self._page_document(
            "Приватность",
            (
                '<div class="page-shell">'
                + back_header("Приватность", "/chat")
                + f'<main class="privacy-main" data-csrf="{escape(csrf)}">'
                '<p id="privacy-feedback" class="toast" role="status" aria-live="polite"></p>'
                '<details class="card"><summary>Уведомление о данных'
                f'<span class="muted"> · {ack_label}</span></summary>{self._notice_text()}</details>'
                f'<section><h2>Мои сообщения</h2>{blocked}<ol class="card-list">'
                + (
                    source_links
                    or '<li class="muted">Пока нет доступных сообщений</li>'
                )
                + "</ol>"
                + source_next
                + '<p class="hint">Откройте сообщение, чтобы разрешить цитату или удалить его.</p></section>'
                '<section><h2>Разрешения на публикацию</h2><ol class="card-list">'
                + ("".join(rows) or '<li class="muted">Пока нет</li>')
                + "</ol>"
                + next_link
                + "</section>"
                + deletions
                + "</main></div>"
            ),
        )

    @staticmethod
    def _next_link(field: str, cursor: str) -> str:
        return f'<a class="button secondary" href="/privacy?{field}={quote(cursor, safe="")}">Показать ещё</a>'

    def _page_sources(self, session, before, grants):
        sources, more = self._source_ids(session, before)
        references = tuple(row[1] for row in grants if not row[6])
        previews = {}
        if references:
            with closing(self.store._open()) as db:
                self.store._verify_current_schema(db)
                placeholders = ",".join("?" for _ in references)
                rows = db.execute(
                    "SELECT e.event_id,p.content FROM event_envelopes e "
                    "JOIN private_payloads p ON p.payload_id=e.payload_id "
                    "WHERE e.audience_principal_id=? AND p.audience_principal_id=e.audience_principal_id "
                    "AND e.event_type='participant_input' AND e.channel='private_chat' "
                    "AND p.retention_status='active' AND e.event_id IN ("
                    + placeholders
                    + ") LIMIT 20",
                    (session.principal_id.value, *references),
                ).fetchall()
                previews = dict(rows)
        return sources, more, previews

    def _deletion_dates(self, session, deletions):
        if not deletions:
            return {}
        with closing(self.ledger._open()) as db:
            placeholders = ",".join("?" for _ in deletions)
            return dict(
                db.execute(
                    "SELECT operation_id,requested_at FROM deletion_records WHERE principal_id=? "
                    "AND operation_id IN (" + placeholders + ") LIMIT 20",
                    (
                        session.principal_id.value,
                        *(row["operation_id"] for row in deletions),
                    ),
                ).fetchall()
            )

    def _source_ids(
        self, session: AuthenticatedSession, before: str | None = None
    ) -> tuple[tuple[tuple[str, str, str], ...], bool]:
        with closing(self.store._open()) as db:
            self.store._verify_current_schema(db)
            cursor = None
            if before is not None:
                cursor = db.execute(
                    "SELECT e.occurred_at,e.event_id FROM event_envelopes e "
                    "JOIN private_payloads p ON p.payload_id=e.payload_id "
                    "WHERE e.event_id=? AND e.audience_principal_id=? "
                    "AND e.event_type='participant_input' AND e.channel='private_chat' "
                    "AND p.audience_principal_id=e.audience_principal_id AND p.retention_status='active'",
                    (before, session.principal_id.value),
                ).fetchone()
                if cursor is None:
                    raise PrivacyControlError(404)
            query = (
                "SELECT e.event_id,p.content,e.occurred_at FROM event_envelopes e "
                "JOIN private_payloads p ON p.payload_id=e.payload_id "
                "WHERE e.audience_principal_id=? AND e.event_type='participant_input' "
                "AND e.channel='private_chat' AND p.audience_principal_id=e.audience_principal_id "
                "AND p.retention_status='active' "
            )
            params: tuple[str, ...] = (session.principal_id.value,)
            if cursor is not None:
                query += "AND (e.occurred_at<? OR (e.occurred_at=? AND e.event_id<?)) "
                params += (cursor[0], cursor[0], cursor[1])
            query += "ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT 21"
            rows = db.execute(query, params).fetchall()
        return tuple(rows[:20]), len(rows) > 20

    def source_page(
        self, session: AuthenticatedSession, csrf: str, reference: str
    ) -> bytes:
        content = self.source(session, reference)
        occurred = self.gate.authorized(lambda: self._source_date(session, reference))
        title = "Сообщение от " + date_label(_time(occurred))
        ref = escape(reference)
        return self._page_document(
            title,
            (
                '<div class="page-shell">'
                + back_header(title, "/privacy")
                + f'<main class="privacy-main" data-csrf="{escape(csrf)}">'
                f'<div class="source-content">{escape(content)}</div>'
                '<p id="privacy-feedback" role="status" aria-live="polite"></p>'
                '<form class="card" method="post" action="/api/privacy/grants" data-endpoint="/api/privacy/grants">'
                "<h2>Разрешить публикацию</h2>"
                '<p class="hint">Разрешение относится только к полному сообщению выше, выбранной цели, публичной аудитории и сроку.</p>'
                f'<input type="hidden" name="source" value="{ref}"><input type="hidden" name="audience" value="public">'
                '<fieldset class="choice-group"><legend>Как</legend><div class="segments">'
                '<label><input type="radio" name="purpose" value="identifiable_quotation" checked>Дословно</label>'
                '<label><input type="radio" name="purpose" value="identifiable_disclosure">С указанием автора</label></div></fieldset>'
                '<fieldset class="choice-group"><legend>На срок</legend><div class="segments">'
                '<label><input type="radio" name="validity_hours" value="1">1 час</label>'
                '<label><input type="radio" name="validity_hours" value="24" checked>Сутки</label>'
                '<label><input type="radio" name="validity_hours" value="168">Неделя</label></div></fieldset>'
                '<p id="purpose-help" class="hint">Агент сможет дословно процитировать это сообщение. '
                "Цитата может быть узнаваемой.</p>"
                '<p class="hint">Публично, в Threads. Разрешение можно отозвать, но уже опубликованное не вернуть.</p>'
                "<button>Разрешить</button></form>"
                '<form class="card danger-zone" method="post" action="/api/privacy/deletions" data-endpoint="/api/privacy/deletions">'
                '<h2>Удалить сообщение</h2><p class="hint">Запросить локальное удаление сообщения и зависимых данных. '
                "Операция может остаться незавершённой.</p>"
                f'<input type="hidden" name="source" value="{ref}">'
                '<input type="hidden" name="confirm" value="delete">'
                '<button class="danger-button">Удалить</button></form></main></div>'
                '<dialog id="delete-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title">'
                '<h2 id="delete-title">Удалить сообщение?</h2>'
                "<p>Будет запрошено удаление сообщения и зависимых данных здесь. Удаление может не завершиться. "
                "У OpenAI, Cloudflare и Meta действуют их правила хранения. Уже опубликованное не возвращается.</p>"
                '<div class="dialog-actions"><button id="delete-cancel" class="secondary" autofocus>Отмена</button>'
                '<button id="delete-confirm" class="danger-button">Удалить</button></div></dialog>'
            ),
        )

    def _source_date(self, session, reference):
        # Recheck the same source under the read gate before returning metadata.
        self._source_unfenced(session, reference)
        with closing(self.store._open()) as db:
            return db.execute(
                "SELECT occurred_at FROM event_envelopes WHERE event_id=? AND audience_principal_id=?",
                (reference, session.principal_id.value),
            ).fetchone()[0]

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
