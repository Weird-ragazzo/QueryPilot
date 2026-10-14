import sqlite3
from pathlib import Path
from typing import Any

ALLOWED_SCHEMA: dict[str, dict[str, str]] = {
    "customers": {"customer_id": "INTEGER", "name": "TEXT", "email": "TEXT", "city": "TEXT", "country": "TEXT", "created_at": "DATE"},
    "products": {"product_id": "INTEGER", "name": "TEXT", "category": "TEXT", "unit_price": "REAL"},
    "orders": {"order_id": "INTEGER", "customer_id": "INTEGER", "order_date": "DATE", "status": "TEXT"},
    "order_items": {"order_item_id": "INTEGER", "order_id": "INTEGER", "product_id": "INTEGER", "quantity": "INTEGER", "unit_price": "REAL"},
}
RELATIONSHIPS = ["orders.customer_id references customers.customer_id", "order_items.order_id references orders.order_id", "order_items.product_id references products.product_id"]


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.executescript("""
        PRAGMA foreign_keys=ON;
        DROP TABLE IF EXISTS order_items; DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS products; DROP TABLE IF EXISTS customers;
        CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, city TEXT NOT NULL, country TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE products(product_id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, unit_price REAL NOT NULL CHECK(unit_price >= 0));
        CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers, order_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('completed','shipped','pending','cancelled')));
        CREATE TABLE order_items(order_item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders, product_id INTEGER NOT NULL REFERENCES products, quantity INTEGER NOT NULL CHECK(quantity > 0), unit_price REAL NOT NULL CHECK(unit_price >= 0));
        """)
        customers = [(i, f"Customer {i:03}", f"customer{i}@example.test", ["New York", "Austin", "Seattle", "Chicago", "Boston"][i % 5], "USA", f"2023-{(i % 12)+1:02}-01") for i in range(1, 61)]
        products = [(i, name, category, price) for i, (name, category, price) in enumerate([
            ("Trail Backpack", "Outdoor", 79.0), ("Insulated Bottle", "Outdoor", 24.5), ("Desk Lamp", "Home", 42.0), ("Cotton Towel Set", "Home", 31.0), ("Wireless Mouse", "Electronics", 35.0), ("USB-C Hub", "Electronics", 59.0), ("Running Socks", "Apparel", 16.0), ("Rain Jacket", "Apparel", 110.0), ("Ceramic Mug", "Home", 18.0), ("Travel Pack", "Outdoor", 95.0)], 1)]
        con.executemany("INSERT INTO customers VALUES(?,?,?,?,?,?)", customers)
        con.executemany("INSERT INTO products VALUES(?,?,?,?)", products)
        orders, items = [], []
        for oid in range(1, 241):
            month = ((oid - 1) % 12) + 1
            day = ((oid * 7 - 1) % 27) + 1
            status = ["completed", "shipped", "pending", "cancelled"][oid % 4]
            orders.append((oid, ((oid * 13) % 60) + 1, f"2025-{month:02}-{day:02}", status))
            for j in range(1, (oid % 3) + 2):
                pid = ((oid * 3 + j * 2) % 10) + 1
                price = products[pid - 1][3]
                items.append((len(items) + 1, oid, pid, ((oid + j) % 4) + 1, price))
        con.executemany("INSERT INTO orders VALUES(?,?,?,?)", orders)
        con.executemany("INSERT INTO order_items VALUES(?,?,?,?,?)", items)
        con.commit()
    finally:
        con.close()


def schema_description() -> dict[str, Any]:
    from .config import get_settings
    # Kept as a compatibility helper; use the same introspector as uploads.
    from .data_sources import inspect_schema
    return {
        **inspect_schema(get_settings().database_path),
        "revenue_definition": "Sum of order_items.quantity * order_items.unit_price for orders whose status is completed or shipped.",
        "sample_date_context": "Orders are from calendar year 2025. Use 2026-01-01 as the sample snapshot date for relative periods: last quarter is 2025 Q4 and last month is December 2025.",
    }


def open_readonly(path: Path, timeout_ms: int = 2000) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError("Sample database is not initialized")
    con = sqlite3.connect(f"{resolved.as_uri()}?mode=ro&immutable=1", uri=True, timeout=timeout_ms / 1000)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con

