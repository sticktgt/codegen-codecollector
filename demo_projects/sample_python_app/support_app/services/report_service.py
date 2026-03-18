"""Отчетные и проекционные функции поверх коллекции тикетов."""

from __future__ import annotations

from collections import Counter

from support_app.domain.models import AgentSummary, Ticket, TicketSummary


def count_open_by_priority(tickets: list[Ticket]) -> dict[str, int]:
    """Считает количество открытых тикетов по каждому приоритету."""
    open_tickets = [ticket for ticket in tickets if ticket.status == 'open']
    return dict(Counter(ticket.priority for ticket in open_tickets))


def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:
    """Строит краткую сводку по открытым тикетам конкретного агента."""
    assigned = [ticket for ticket in tickets if ticket.assigned_to == agent_name and ticket.status == 'open']
    urgent_count = sum(1 for ticket in assigned if ticket.priority == 'high')
    return AgentSummary(agent_name=agent_name, open_tickets=len(assigned), urgent_tickets=urgent_count)


def to_ticket_summaries(tickets: list[Ticket]) -> list[TicketSummary]:
    """Преобразует полные тикеты в короткие карточки для списка."""
    return [
        TicketSummary(
            ticket_id=ticket.ticket_id,
            title=ticket.title,
            priority=ticket.priority,
            assigned_to=ticket.assigned_to,
        )
        for ticket in tickets
    ]
