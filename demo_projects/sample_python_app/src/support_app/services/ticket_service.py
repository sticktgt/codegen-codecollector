"""Сервисные операции для создания, назначения и закрытия тикетов."""

from __future__ import annotations

from support_app.domain.models import Ticket, TicketSummary
from support_app.services.notification_service import build_assignment_message
from support_app.services.report_service import to_ticket_summaries
from support_app.storage.ticket_repository import TicketRepository


class TicketService:
    """Инкапсулирует основную бизнес-логику работы с тикетами."""

    def __init__(self, repository: TicketRepository) -> None:
        self.repository = repository

    def create_ticket(self, ticket: Ticket) -> Ticket:
        """Сохраняет новый тикет в репозитории и возвращает сохраненный объект."""
        return self.repository.save(ticket)

    def list_open_tickets(self) -> list[TicketSummary]:
        """Возвращает краткие карточки только для открытых тикетов."""
        open_tickets = [ticket for ticket in self.repository.list_all() if ticket.status == 'open']
        return to_ticket_summaries(open_tickets)

    def assign_ticket(self, ticket_id: str, agent_name: str) -> str:
        """Назначает тикет агенту, сохраняет изменения и формирует текст уведомления."""
        ticket = self.repository.get(ticket_id)
        if ticket is None:
            raise ValueError(f'Unknown ticket: {ticket_id}')
        ticket.assigned_to = agent_name
        self.repository.save(ticket)
        return build_assignment_message(ticket, agent_name)

    def close_ticket(self, ticket_id: str) -> Ticket:
        """Переводит тикет в состояние `closed` и возвращает обновленный объект."""
        ticket = self.repository.get(ticket_id)
        if ticket is None:
            raise ValueError(f'Unknown ticket: {ticket_id}')
        ticket.status = 'closed'
        return self.repository.save(ticket)
