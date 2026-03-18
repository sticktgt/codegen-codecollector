"""Dataclass-модели предметной области demo-приложения поддержки."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Ticket:
    """Полное представление тикета службы поддержки."""

    ticket_id: str
    title: str
    description: str
    priority: str = 'medium'
    status: str = 'open'
    assigned_to: str | None = None
    created_at: datetime | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TicketSummary:
    """Короткая карточка тикета для списков и компактных ответов."""

    ticket_id: str
    title: str
    priority: str
    assigned_to: str | None


@dataclass(slots=True)
class AgentSummary:
    """Сводка по количеству открытых и срочных тикетов агента."""

    agent_name: str
    open_tickets: int
    urgent_tickets: int
