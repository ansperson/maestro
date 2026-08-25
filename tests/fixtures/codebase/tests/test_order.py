def test_order_accepts_more_than_one_payment() -> None:
    order = Order()
    order.payments.extend([Payment(), Payment()])
    assert len(order.payments) == 2
