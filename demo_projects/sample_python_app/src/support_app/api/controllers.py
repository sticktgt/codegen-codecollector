"""Контроллеры демонстрационного API для работы с тикетами и сводками."""

from __future__ import annotations

from support_app.domain.models import Ticket
from support_app.services.report_service import build_agent_summary
from support_app.services.ticket_service import TicketService


class TicketController:
    """Адаптирует внешние payload-запросы к сервисному слою demo-приложения."""

    def __init__(self, service: TicketService) -> None:
        self.service = service

    def create_ticket_endpoint(self, payload: dict) -> Ticket:
        """Создает тикет из входного payload и делегирует сохранение сервису."""
        ticket = Ticket(
            ticket_id=payload['ticket_id'],
            title=payload['title'],
            description=payload['description'],
            priority=payload.get('priority', 'medium'),
        )
        return self.service.create_ticket(ticket)

    def assign_ticket_endpoint(self, ticket_id: str, agent_name: str) -> dict:
        """Назначает тикет агенту и возвращает компактный ответ API-уровня."""
        message = self.service.assign_ticket(ticket_id, agent_name)
        return {'ticket_id': ticket_id, 'assigned_to': agent_name, 'message': message}

    def agent_summary_endpoint(self, agent_name: str) -> dict:
        """Возвращает краткую сводку по открытым тикетам выбранного агента."""
        tickets = self.service.repository.list_by_agent(agent_name)
        summary = build_agent_summary(agent_name, tickets)
        return summary.__dict__
