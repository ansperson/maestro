class Refund:
    """A refund issued against a payment.

    The window this may be issued within is not decided here; see the two
    conflicting statements in docs/ and migrations/.
    """

    def __init__(self, payment_id: int, amount: int) -> None:
        self.payment_id = payment_id
        self.amount = amount
