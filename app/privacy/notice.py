"""Versioned Russian pre-enrollment privacy notice and acknowledgment API."""

from datetime import datetime

from .ledger import PrivacyLedger
from .models import (
    AuditRecord,
    OperationOrigin,
    OperationResult,
    TrustedPrivacyContext,
    require_token,
    require_utc,
)


NOTICE_VERSION = "privacy-notice-ru-v1"

NOTICE_RU = """До начала общения важно знать, как обрабатываются данные.

Содержание приватного разговора может передаваться уполномоченному поставщику
модели OpenAI. Cloudflare обрабатывает сетевой трафик настроенного туннеля. Если
материал будет опубликован в Threads, его и ответы на него обрабатывает Meta.
Владелец и оператор инфраструктуры технически могут иметь доступ к локальному
хранилищу; это не даёт им права раскрывать сведения участника через приложение.

Идеи из приватного разговора могут влиять на общее интеллектуальное развитие в
виде безопасной абстракции. Сырой текст, персональные сведения, узнаваемый
пересказ, отношения и авторство автоматически не раскрываются. При двух
участниках другой человек иногда может сделать вывод о вероятном источнике
влияния. Для идентифицируемой цитаты или раскрытия требуется отдельное точное,
ограниченное по аудитории, цели и сроку согласие через защищённый интерфейс.

Можно запросить отзыв согласия и локальное удаление подходящих данных. Отзыв не
возвращает уже опубликованное. Локальное удаление не означает удаление у OpenAI,
Cloudflare, Meta, получателей, в кэшах и других внешних системах. Их фактические
сроки хранения, регион обработки и обязательства должны быть подтверждены до
регистрации реальных участников.
"""

OPERATOR_LEGAL_DECISIONS_RU = (
    "правовое основание и юрисдикция обработки",
    "точные условия и сроки хранения у каждого внешнего обработчика",
    "локальные сроки хранения, SLA удаления, исключения и legal hold",
    "политика резервных копий, шифрование и жизненный цикл ключей",
    "окончательный текст уведомления и интерфейс подтверждения/отзыва",
)


class NoticeService:
    def __init__(self, ledger: PrivacyLedger):
        self.ledger = ledger

    def acknowledge(
        self,
        context: TrustedPrivacyContext,
        notice_version: str,
        acknowledged_at: datetime,
        correlation_id: str,
    ) -> OperationResult:
        require_token(correlation_id, "correlation id")
        require_utc(acknowledged_at, "acknowledgement timestamp")
        if notice_version != NOTICE_VERSION:
            raise ValueError("unsupported notice version")
        if context.origin is not OperationOrigin.CONTROL:
            self.ledger.record_audit(
                AuditRecord(
                    correlation_id,
                    context.actor_reference,
                    "notice_acknowledgement",
                    "privacy_notice",
                    acknowledged_at,
                    "invalid_authentication",
                    "payload_free",
                )
            )
            return OperationResult(False, "invalid_authentication")
        created = self.ledger.acknowledge_notice(
            context.principal_id,
            notice_version,
            context.actor_reference,
            context.authentication_event_id,
            acknowledged_at,
            correlation_id,
        )
        return OperationResult(
            created, "acknowledged" if created else "already_acknowledged"
        )

    def has_acknowledged(self, principal_id: str, notice_version: str) -> bool:
        require_token(principal_id, "principal id")
        require_token(notice_version, "notice version")
        return self.ledger.has_notice(principal_id, notice_version)
