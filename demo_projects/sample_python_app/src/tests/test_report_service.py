from support_app.domain.models import Ticket
from support_app.services.report_service import build_agent_summary, count_open_by_priority


def test_count_open_by_priority_only_counts_open() -> None:
    tickets = [
        Ticket(ticket_id='T-1', title='A', description='A', priority='high', status='open'),
        Ticket(ticket_id='T-2', title='B', description='B', priority='low', status='closed'),
        Ticket(ticket_id='T-3', title='C', description='C', priority='high', status='open'),
    ]

    counts = count_open_by_priority(tickets)

    assert counts == {'high': 2}


def test_build_agent_summary_counts_only_open_assigned() -> None:
    tickets = [
        Ticket(ticket_id='T-1', title='A', description='A', priority='high', status='open', assigned_to='alice'),
        Ticket(ticket_id='T-2', title='B', description='B', priority='low', status='closed', assigned_to='alice'),
    ]

    summary = build_agent_summary('alice', tickets)

    assert summary.open_tickets == 1
    assert summary.urgent_tickets == 1
