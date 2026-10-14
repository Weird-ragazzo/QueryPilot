"""Unit tests and regression protection for QueryPilot evaluation framework."""
from __future__ import annotations

import json
from pathlib import Path

from evaluation.comparison import compare_results, _values_equal, _rows_match
from evaluation.dataset import ensure_benchmark_fixtures, compute_golden_result
from evaluation.instrumentation import classify_outcome_stage, inspect_error_for_leaks, ExecutionSpyRecord
from evaluation.metrics import calculate_percentile, calculate_latency_stats, compute_aggregate_metrics
from evaluation.runner import run_evaluation


def test_benchmark_dataset_fixtures_exist():
    """Verify deterministic benchmark databases are generated properly."""
    retail_db, hr_db = ensure_benchmark_fixtures()
    assert retail_db.exists()
    assert hr_db.exists()

    # Verify key tables exist in retail fixture
    import sqlite3
    con = sqlite3.connect(f"{retail_db.as_uri()}?mode=ro&immutable=1", uri=True)
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    con.close()

    assert {"customers", "products", "orders", "order_items"}.issubset(tables)


def test_golden_answer_verification():
    """Verify reference SQL runs deterministically on benchmark fixture."""
    retail_db, _ = ensure_benchmark_fixtures()
    sql = "SELECT COUNT(*) AS total_customers FROM customers"
    rows = compute_golden_result(retail_db, sql)
    assert len(rows) == 1
    assert rows[0]["total_customers"] == 30


def test_result_comparison_exact_and_order_sensitivity():
    """Verify result set comparison handles order when required and when not."""
    r1 = [{"id": 1, "val": "A"}, {"id": 2, "val": "B"}]
    r2 = [{"id": 2, "val": "B"}, {"id": 1, "val": "A"}]

    # Order does not matter: match
    comp_unordered = compare_results(r1, r2, order_matters=False)
    assert comp_unordered.matched is True

    # Order matters: mismatch
    comp_ordered = compare_results(r1, r2, order_matters=True)
    assert comp_ordered.matched is False


def test_result_comparison_numeric_tolerances():
    """Verify floating point tolerances handle slight precision differences."""
    r1 = [{"revenue": 100.004}]
    r2 = [{"revenue": 100.00}]
    assert compare_results(r1, r2, float_tolerance=0.01).matched is True
    assert compare_results(r1, [{"revenue": 105.0}], float_tolerance=0.01).matched is False


def test_result_comparison_null_and_empty():
    """Verify NULL values and empty result sets."""
    # Empty result sets
    assert compare_results([], []).matched is True
    assert compare_results([{"a": 1}], []).matched is False

    # NULL handling
    assert compare_results([{"city": None}], [{"city": None}]).matched is True
    assert compare_results([{"city": None}], [{"city": "Chicago"}]).matched is False


def test_percentile_calculations():
    """Verify statistical percentile calculations."""
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert calculate_percentile(vals, 50.0) == 30.0
    assert calculate_percentile(vals, 0.0) == 10.0
    assert calculate_percentile(vals, 100.0) == 50.0
    stats = calculate_latency_stats(vals)
    assert stats["count"] == 5
    assert stats["mean_ms"] == 30.0
    assert stats["min_ms"] == 10.0
    assert stats["max_ms"] == 50.0


def test_aggregate_metrics_calculations():
    """Verify correctness and security aggregate metrics calculations."""
    mock_results = [
        # Answerable: 1 success, 1 fail
        {
            "expected_outcome": "success",
            "actual_status": "success",
            "result_matched": True,
            "sql_validated": True,
            "is_safety_case": False,
            "latency_ms": 100.0,
            "sql_duration_ms": 1.0,
        },
        {
            "expected_outcome": "success",
            "actual_status": "error",
            "result_matched": False,
            "sql_validated": False,
            "is_safety_case": False,
            "stage": "provider_failure",
            "latency_ms": 200.0,
            "sql_duration_ms": 0.0,
        },
        # Safety case: blocked
        {
            "expected_outcome": "refused",
            "actual_status": "refused",
            "result_matched": True,
            "sql_validated": False,
            "is_safety_case": True,
            "forbidden_execution": False,
            "stage": "pre_llm_guardrail",
            "latency_ms": 10.0,
            "sql_duration_ms": 0.0,
        },
    ]

    metrics = compute_aggregate_metrics(mock_results)
    corr = metrics["correctness_and_reliability"]
    sec = metrics["security_and_guardrails"]

    assert corr["exact_result_accuracy"]["value"] == 0.5
    assert corr["exact_result_accuracy"]["numerator"] == 1
    assert corr["exact_result_accuracy"]["denominator"] == 2
    assert sec["total_unsafe_cases"] == 1
    assert sec["unsafe_cases_correctly_blocked"] == 1
    assert sec["forbidden_execution_count"] == 0
    assert sec["security_violation_rate"] == 0.0


def test_leak_detection():
    """Verify sensitive leak regex detector."""
    assert inspect_error_for_leaks("Normal user query error") is False
    assert inspect_error_for_leaks("Failed at /home/user/project/file.py") is True
    assert inspect_error_for_leaks("sqlite3.OperationalError: table locked") is True


def test_stage_classification():
    """Verify outcome stage classifier."""
    spy = ExecutionSpyRecord()
    assert classify_outcome_stage({"error_code": "READ_ONLY_OPERATION_BLOCKED"}, spy) == "pre_llm_guardrail"
    assert classify_outcome_stage({"status": "refused", "error": "This question cannot be answered"}, spy) == "schema_refusal"
    assert classify_outcome_stage({"status": "refused", "error": "Only read-only SELECT queries are allowed"}, spy) == "ast_validation"
    assert classify_outcome_stage({"status": "error", "error": "Rate limit 429"}, spy) == "provider_rate_limit"
    assert classify_outcome_stage({"status": "success"}, spy) == "completed"


def test_offline_runner_execution(tmp_path):
    """Verify offline evaluation runs end-to-end and outputs reports."""
    report = run_evaluation(
        output_dir=tmp_path / "reports",
        mode="offline",
        max_requests=5,
    )
    assert report["benchmark_metadata"]["mode"] == "offline"
    assert report["benchmark_metadata"]["total_evaluated_cases"] == 5
    assert (tmp_path / "reports" / "latest.json").exists()
    assert (tmp_path / "reports" / "latest.csv").exists()
    assert (tmp_path / "reports" / "latest.md").exists()
