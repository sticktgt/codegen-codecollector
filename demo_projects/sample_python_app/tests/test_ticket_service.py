from support_app.domain.models import Ticket
from support_app.services.ticket_service import TicketService
from support_app.storage.ticket_repository import TicketRepository


def test_assign_ticket_returns_message() -> None:
    repository = TicketRepository()
    service = TicketService(repository)
    service.create_ticket(Ticket(ticket_id='T-1', title='Broken button', description='Save button does not work'))

    message = service.assign_ticket('T-1', 'alice')

    assert 'alice' in message
    assert repository.get('T-1').assigned_to == 'alice'


def test_list_open_tickets_filters_closed_items() -> None:
    repository = TicketRepository()
    service = TicketService(repository)
    service.create_ticket(Ticket(ticket_id='T-1', title='Broken button', description='Save button does not work'))
    service.create_ticket(Ticket(ticket_id='T-2', title='Timeout', description='Client request timeout', status='closed'))

    summaries = service.list_open_tickets()

    assert len(summaries) == 1
    assert summaries[0].ticket_id == 'T-1'
