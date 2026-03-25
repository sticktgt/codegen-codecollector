from __future__ import annotations

from collections import Counter

from support_app.domain.models import AgentSummary, Ticket, TicketSummary


def count_open_by_priority(tickets: list[Ticket]) -> dict[str, int]:
    open_tickets = [ticket for ticket in tickets if ticket.status == 'open']
    return dict(Counter(ticket.priority for ticket in open_tickets))


def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:
    assigned = [ticket for ticket in tickets if ticket.assigned_to == agent_name and ticket.status == 'open']
    urgent_count = sum(1 for ticket in assigned if ticket.priority == 'high')
    return AgentSummary(agent_name=agent_name, open_tickets=len(assigned), urgent_tickets=urgent_count)


def to_ticket_summaries(tickets: list[Ticket]) -> list[TicketSummary]:
    return [
        TicketSummary(
            ticket_id=ticket.ticket_id,
            title=ticket.title,
            priority=ticket.priority,
            assigned_to=ticket.assigned_to,
        )
        for ticket in tickets
    ]

def build_priority_label(priority: str) -> str:
    """Возвращает человекочитаемую русскоязычную метку приоритета тикета."""
    labels = {
        'low': 'Низкий приоритет',
        'medium': 'Средний приоритет',
        'high': 'Высокий приоритет',
    }
    return labels.get(priority, 'Приоритет не указан')

