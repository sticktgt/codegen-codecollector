"""Переиспользуемые helpers для русскоязычных уведомлений о тикетах."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MessageTone:
    """Настройки тона сообщения для конечного пользователя."""

    concise: bool = True
    use_exclamation: bool = False


def _suffix(tone: MessageTone) -> str:
    """Возвращает завершающий суффикс с учетом тона сообщения."""
    return "!" if tone.use_exclamation else "."


def build_assignment_message_ru(ticket_id: str, assignee: str, tone: MessageTone | None = None) -> str:
    """Формирует русскоязычное уведомление о назначении тикета.

    Этот helper подходит как почти готовый компонент для переиспользования
    или как reference implementation для адаптации внутри проекта.
    """
    tone = tone or MessageTone()
    if tone.concise:
        return f"Тикет {ticket_id} теперь назначен на {assignee}{_suffix(tone)}"
    return f"Тикет {ticket_id} успешно назначен сотруднику {assignee}{_suffix(tone)}"


def build_status_change_message_ru(ticket_id: str, status_label: str) -> str:
    """Формирует краткое сообщение об изменении статуса тикета."""
    return f"Статус тикета {ticket_id} изменен: {status_label}."


def build_reassignment_message_ru(ticket_id: str, previous_assignee: str, new_assignee: str) -> str:
    """Формирует сообщение о переназначении тикета."""
    return (
        f"Тикет {ticket_id} переназначен с {previous_assignee} "
        f"на {new_assignee}."
    )
