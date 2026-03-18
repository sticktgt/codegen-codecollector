def build_assignment_audit_line(ticket, agent_name):
    """Создать дополнительную строку для журналирования назначения."""
    return f"AUDIT assignment ticket={ticket.ticket_id} agent={agent_name}"
