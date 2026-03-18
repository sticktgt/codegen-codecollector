"""Формирование текстовых уведомлений, связанных с тикетами."""

from __future__ import annotations

from support_app.domain.models import Ticket


def build_assignment_message(ticket: Ticket, agent_name: str) -> str:
    """Формирует короткое уведомление после назначения тикета агенту."""
    return f"Ticket {ticket.ticket_id} assigned to {agent_name}."
