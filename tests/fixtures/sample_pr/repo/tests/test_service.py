from orders.service import apply_discount


def test_ten_percent():
    assert apply_discount(100, 10) == 90
