"""Deterministic isolated benchmark dataset generator for QueryPilot evaluation."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_BENCHMARK_PATH = Path("evaluation/fixtures/benchmark.sqlite3")
DEFAULT_HR_BENCHMARK_PATH = Path("evaluation/fixtures/hr_fixture.sqlite3")


def create_retail_benchmark_db(db_path: Path = DEFAULT_BENCHMARK_PATH) -> Path:
    """Create a deterministic retail database with customers, products, orders, order_items."""
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            city TEXT,
            country TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            unit_price REAL NOT NULL CHECK(unit_price >= 0),
            discount_pct REAL
        );

        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
            order_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('completed', 'shipped', 'pending', 'cancelled'))
        );

        CREATE TABLE order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(order_id),
            product_id INTEGER NOT NULL REFERENCES products(product_id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price REAL NOT NULL CHECK(unit_price >= 0)
        );
        """)

        # 30 deterministic customers
        cities = ["Chicago", "Austin", "New York", "Seattle", "Boston", "Denver"]
        customers = []
        for i in range(1, 31):
            city = None if i == 30 else cities[(i - 1) % len(cities)]
            email = f"customer{i:02d}@benchmark.test"
            month = ((i * 3) % 12) + 1
            day = ((i * 5) % 27) + 1
            year = 2023 if i <= 15 else 2024
            customers.append((
                i,
                f"Customer {i:02d}",
                email,
                city,
                "USA",
                f"{year}-{month:02d}-{day:02d}",
            ))
        con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customers)

        # 12 deterministic products (product 12 is never ordered)
        products_data = [
            (1, "Wireless Mouse", "Electronics", 35.0, 0.05),
            (2, "USB-C Hub", "Electronics", 59.0, 0.10),
            (3, "Mechanical Keyboard", "Electronics", 120.0, None),
            (4, "Desk Lamp", "Home", 42.0, None),
            (5, "Ceramic Mug", "Home", 18.0, 0.15),
            (6, "Cotton Towel Set", "Home", 31.0, None),
            (7, "Trail Backpack", "Outdoor", 79.0, 0.10),
            (8, "Insulated Bottle", "Outdoor", 24.5, None),
            (9, "Travel Pack", "Outdoor", 95.0, 0.20),
            (10, "Running Socks", "Apparel", 16.0, None),
            (11, "Rain Jacket", "Apparel", 110.0, 0.05),
            (12, "Fleece Beanie", "Apparel", 22.0, None),  # Never ordered
        ]
        con.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", products_data)

        # 100 deterministic orders in 2025
        orders = []
        order_items = []
        item_id = 1
        status_pool = ["completed", "completed", "shipped", "completed", "pending", "cancelled"]

        for oid in range(1, 101):
            cid = ((oid * 7) % 30) + 1
            month = ((oid - 1) % 12) + 1
            day = ((oid * 11) % 27) + 1
            status = status_pool[oid % len(status_pool)]
            order_date = f"2025-{month:02d}-{day:02d}"
            orders.append((oid, cid, order_date, status))

            # Each order has 1 to 3 items, selected from products 1..11
            num_items = (oid % 3) + 1
            for j in range(num_items):
                pid = ((oid * 2 + j * 3) % 11) + 1
                qty = ((oid + j) % 4) + 1
                price = products_data[pid - 1][3]
                order_items.append((item_id, oid, pid, qty, price))
                item_id += 1

        con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", orders)
        con.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", order_items)
        con.commit()
    finally:
        con.close()

    return db_path


def create_hr_benchmark_db(db_path: Path = DEFAULT_HR_BENCHMARK_PATH) -> Path:
    """Create a separate small deterministic HR database for multi-schema evaluation."""
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    con = sqlite3.connect(db_path)
    try:
        con.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE departments (
            dept_id INTEGER PRIMARY KEY,
            dept_name TEXT NOT NULL,
            budget REAL NOT NULL CHECK(budget >= 0)
        );

        CREATE TABLE employees (
            emp_id INTEGER PRIMARY KEY,
            dept_id INTEGER NOT NULL REFERENCES departments(dept_id),
            name TEXT NOT NULL,
            title TEXT NOT NULL,
            salary REAL NOT NULL CHECK(salary >= 0),
            hire_date TEXT NOT NULL
        );
        """)

        departments = [
            (1, "Engineering", 750000.0),
            (2, "Product", 400000.0),
            (3, "Sales", 600000.0),
            (4, "Operations", 300000.0),
        ]
        employees = [
            (1, 1, "Alice Smith", "Lead Engineer", 145000.0, "2021-03-15"),
            (2, 1, "Bob Jones", "Senior Backend Engineer", 125000.0, "2022-06-01"),
            (3, 1, "Charlie Brown", "Frontend Engineer", 105000.0, "2023-01-10"),
            (4, 2, "Diana Prince", "Director of Product", 160000.0, "2020-11-01"),
            (5, 2, "Ethan Hunt", "Product Manager", 115000.0, "2023-04-20"),
            (6, 3, "Fiona Gallagher", "VP of Sales", 175000.0, "2019-08-12"),
            (7, 3, "George Clark", "Account Executive", 90000.0, "2022-09-05"),
            (8, 4, "Hannah Abbott", "Operations Manager", 95000.0, "2021-10-18"),
        ]
        con.executemany("INSERT INTO departments VALUES (?, ?, ?)", departments)
        con.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?)", employees)
        con.commit()
    finally:
        con.close()

    return db_path


def compute_golden_result(db_path: Path, reference_sql: str) -> list[dict[str, Any]]:
    """Execute reference SQL against the benchmark database and return golden result rows."""
    con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(reference_sql)
        return [dict(row) for row in cur.fetchall()]
    finally:
        con.close()


def ensure_benchmark_fixtures() -> tuple[Path, Path]:
    """Ensure both retail and HR benchmark fixtures exist on disk."""
    retail = DEFAULT_BENCHMARK_PATH.resolve()
    hr = DEFAULT_HR_BENCHMARK_PATH.resolve()
    if not retail.exists():
        create_retail_benchmark_db(retail)
    if not hr.exists():
        create_hr_benchmark_db(hr)
    return retail, hr


if __name__ == "__main__":
    r_path, h_path = ensure_benchmark_fixtures()
    print(f"Created retail benchmark database: {r_path}")
    print(f"Created HR benchmark database: {h_path}")
