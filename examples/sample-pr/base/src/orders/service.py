import sqlite3


def list_orders(conn: sqlite3.Connection) -> list[tuple]:
    """Return all orders, newest first."""
    return conn.execute("SELECT id, customer, total FROM orders ORDER BY id DESC").fetchall()
