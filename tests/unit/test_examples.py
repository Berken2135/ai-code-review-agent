"""The sample PR for live runs must keep demonstrating what it claims to demonstrate."""

import importlib.util
import sqlite3
from pathlib import Path

EXAMPLE = Path(__file__).parents[2] / "examples" / "sample-pr"


def load_service(folder: str):
    path = EXAMPLE / folder / "src" / "orders" / "service.py"
    spec = importlib.util.spec_from_file_location(f"sample_{folder}_service", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)")
    conn.executemany(
        "INSERT INTO orders (customer, total) VALUES (?, ?)", [("ada", 10.0), ("bob", 25.5)]
    )
    return conn


def test_base_branch_is_clean_and_tested():
    base = load_service("base")

    assert not hasattr(base, "get_order") and not hasattr(base, "apply_discount")
    assert "list_orders" in (EXAMPLE / "base" / "tests" / "test_service.py").read_text()
    assert [o[1] for o in base.list_orders(make_db())] == ["bob", "ada"]


def test_the_pr_change_contains_the_deliberate_sql_injection():
    change = load_service("change")
    conn = make_db()

    assert change.get_order(conn, "1")[1] == "ada"  # works normally
    assert change.get_order(conn, "999") is None
    # An id that should match nothing returns a row: the query is built by string concatenation.
    assert change.get_order(conn, "999' OR '1'='1") is not None


def test_the_pr_change_contains_the_unvalidated_discount_and_adds_no_tests():
    change = load_service("change")

    assert change.apply_discount(100, 10) == 90
    assert change.apply_discount(100, 150) == -50  # negative price: no validation
    assert not (EXAMPLE / "change" / "tests").exists()  # the missing test is deliberate
