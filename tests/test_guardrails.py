import pytest

from app.guardrails import SQLRejected, validate_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) AS order_count FROM orders",
        "SELECT status, COUNT(order_id) AS order_count FROM orders GROUP BY status ORDER BY order_count DESC LIMIT 5",
        "SELECT SUM(quantity * unit_price) AS revenue FROM order_items",
        "SELECT order_id FROM orders WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01'",
        "SELECT o.order_id, oi.quantity FROM orders AS o JOIN order_items AS oi ON oi.order_id = o.order_id LIMIT 10",
        "SELECT name FROM customers WHERE country = 'USA'",
        "SELECT c.name AS customer_name, COUNT(o.order_id) AS order_count FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.name ORDER BY order_count DESC LIMIT 10",
        "SELECT category, SUM(quantity * oi.unit_price) AS revenue FROM products p JOIN order_items oi ON oi.product_id = p.product_id GROUP BY category ORDER BY revenue DESC LIMIT 10",
        "WITH recent_orders AS (SELECT order_id FROM orders WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01') SELECT COUNT(*) AS order_count FROM recent_orders",
        "SELECT r.order_count FROM (SELECT customer_id, COUNT(*) AS order_count FROM orders GROUP BY customer_id) AS r ORDER BY r.order_count DESC LIMIT 5",
        "SELECT 1 WHERE 0",
    ],
)
def test_valid_select_shapes_are_accepted(sql):
    assert validate_sql(sql).upper().startswith(("SELECT", "WITH"))


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT order_id FROM missing_orders", "table"),
        ("SELECT missing_column FROM orders", "column"),
        ("SELECT o.missing_column FROM orders AS o", "column"),
        ("SELECT order_id FROM orders; SELECT customer_id FROM orders", "one SQL statement"),
        ("DELETE FROM orders", "read-only SELECT"),
        ("UPDATE orders SET status = 'shipped'", "read-only SELECT"),
        ("INSERT INTO orders(order_id) VALUES (999)", "read-only SELECT"),
        ("DROP TABLE orders", "read-only SELECT"),
        ("SELECT * FROM sqlite_master", "table"),
    ],
)
def test_invalid_or_unsafe_queries_are_rejected(sql, message):
    with pytest.raises(SQLRejected, match=message):
        validate_sql(sql)


def test_ambiguous_unqualified_join_column_is_rejected():
    sql = "SELECT customer_id FROM orders JOIN customers ON orders.customer_id = customers.customer_id"
    with pytest.raises(SQLRejected, match="ambiguous column"):
        validate_sql(sql)


def test_validation_allows_only_views_in_canonical_view_schema():
    schema = {
        "tables": {"employee": {"columns": {"full_name": {}, "department_id": {}}}},
        "views": {"employee_directory": {"columns": {"full_name": {}, "department_name": {}}}},
    }
    assert "employee_directory" in validate_sql("SELECT full_name FROM employee_directory", schema)
    for sql in ("SELECT private_note FROM employee_directory", "SELECT * FROM unapproved_view"):
        with pytest.raises(SQLRejected):
            validate_sql(sql, schema)
