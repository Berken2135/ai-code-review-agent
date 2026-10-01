import sqlite3

from orders.service import list_orders


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)")
    conn.executemany(
        "INSERT INTO orders (customer, total) VALUES (?, ?)", [("ada", 10.0), ("bob", 25.5)]
    )
    return conn


def test_list_orders_returns_newest_first():
    orders = list_orders(make_db())

    assert [o[1] for o in orders] == ["bob", "ada"]
