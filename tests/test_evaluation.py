import json
from app.evaluation import normalize, run_benchmark

def test_result_comparison_ignores_row_order_and_mapping_order():
    assert normalize([{"a":1,"b":"x"},{"a":2,"b":"y"}]) == normalize([{"b":"y","a":2},{"b":"x","a":1}])

def test_benchmark_runner_writes_machine_readable_report(tmp_path):
    from app.database import initialize_database
    from app.config import get_settings
    settings = get_settings()
    original = settings.database_path
    settings.database_path = tmp_path / "retail.sqlite3"
    initialize_database(settings.database_path)
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(json.dumps({"version":"test", "cases":[{"id":"count","question":"How many?","kind":"answerable","reference_sql":"SELECT COUNT(*) AS count FROM customers"}]}))
    report_path = tmp_path / "report.json"
    try:
        report = run_benchmark(benchmark, report_path, query_fn=lambda _q: {"status":"success", "rows":[{"count":60}], "latency_ms":1.5})
        assert report["metrics"]["result_set_accuracy"] == 1
        assert report["denominators"]["answerable"] == 1
        assert json.loads(report_path.read_text())["cases"][0]["result_match"] is True
    finally:
        settings.database_path = original

