"""What an operator can do from the console pages that act on the queue, assessments, review
queues, costs, uploads and discovered domains (spec B11.5). Rules live here, not in the routes, so
tests and scripts can use them; every change writes its audit row in the same transaction."""
