def build_assignment_message(ticket: Ticket, agent_name: str) -> str:
    """Формирует русскоязычное и понятное уведомление о назначении тикета агенту."""
    return f"Тикет {ticket.ticket_id} теперь назначен на {agent_name}."
