import sqlite3


def list_orders(conn: sqlite3.Connection) -> list[tuple]:
    """Return all orders, newest first."""
    return conn.execute("SELECT id, customer, total FROM orders ORDER BY id DESC").fetchall()


def get_order(conn: sqlite3.Connection, order_id: str):
    """Return one order by id, or None."""
    query = "SELECT id, customer, total FROM orders WHERE id = '" + order_id + "'"
    return conn.execute(query).fetchone()


def apply_discount(total: float, percent: float) -> float:
    """Return `total` reduced by `percent` percent."""
    return total - total * percent / 100
