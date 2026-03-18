    def reopen_ticket(self, ticket_id: str) -> Ticket:
        ticket = self.repository.get(ticket_id)
        if ticket is None:
            raise ValueError(f'Unknown ticket: {ticket_id}')
        ticket.status = 'open'
        return self.repository.save(ticket)
