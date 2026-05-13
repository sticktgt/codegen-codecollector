"""Простой in-memory репозиторий тикетов для демонстрационного проекта."""

from __future__ import annotations

from support_app.domain.models import Ticket


class TicketRepository:
    """Хранит тикеты в памяти и предоставляет базовые операции выборки."""

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}

    def save(self, ticket: Ticket) -> Ticket:
        """Сохраняет тикет по его идентификатору и возвращает сохраненный объект."""
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        """Возвращает тикет по идентификатору или `None`, если он не найден."""
        return self._tickets.get(ticket_id)

    def list_all(self) -> list[Ticket]:
        """Возвращает все тикеты, которые сейчас хранятся в репозитории."""
        return list(self._tickets.values())

    def list_by_agent(self, agent_name: str) -> list[Ticket]:
        """Возвращает тикеты, назначенные указанному агенту."""
        return [ticket for ticket in self._tickets.values() if ticket.assigned_to == agent_name]
