import io
import json
import sqlite3

import pytest
from openpyxl import Workbook

from app.config import get_settings
from app.data_sources import register_upload
from app.schemas import ModelQuery
from app.workflow import build_visualization, classify_intent, run_query


@pytest.fixture
def upload_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    get_settings().max_upload_bytes = 1024 * 1024
    get_settings().max_databases_per_session = 5
    return tmp_path


def xlsx_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Department Metrics"
    sheet.append(["department", "employees", "revenue", "report_date"])
    sheet.append(["Engineering", 5, 100.0, "2025-01-01"])
    sheet.append(["Sales", 3, 60.0, "2025-01-02"])
    sheet.append(["Sales", 4, None, "2025-01-03"])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def csv_bytes():
    return b"department,employees,revenue,report_date\nEngineering,5,100,2025-01-01\nSales,3,60,2025-01-02\nSales,4,,2025-01-03\n"


def sqlite_bytes(tmp_path):
    path = tmp_path / "metrics.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE department_metrics(department TEXT, employees INTEGER, revenue REAL, report_date TEXT);
        INSERT INTO department_metrics VALUES ('Engineering',5,100.0,'2025-01-01'), ('Sales',3,60.0,'2025-01-02'), ('Sales',4,NULL,'2025-01-03');
    """)
    connection.commit()
    connection.close()
    return path.read_bytes()


@pytest.mark.parametrize("question,expected", [
    ("Show a pie chart of revenue by department", ("visualization", "pie")),
    ("Plot a line chart of revenue by month", ("visualization", "line")),
    ("Summarize this dataset", ("summary", None)),
    ("Compare employee counts across departments", ("comparison", None)),
    ("How many employees are there?", ("query", None)),
])
def test_intent_plan(question, expected):
    plan = classify_intent(question)
    assert (plan.intent, plan.requested_chart) == expected


def test_explicit_pie_and_line_charts_use_real_result_columns():
    rows = [{"department":"Engineering","revenue":100}, {"department":"Sales","revenue":60}]
    pie = build_visualization(classify_intent("Show a pie chart of revenue by department").model_dump(), ["department", "revenue"], rows)
    assert (pie["type"], pie["label_column"], pie["series_columns"]) == ("pie", "department", ["revenue"])
    line_rows = [{"month":"2025-01","revenue":100}, {"month":"2025-02","revenue":120}]
    line = build_visualization(classify_intent("Plot a line chart of revenue by month").model_dump(), ["month", "revenue"], line_rows)
    assert (line["type"], line["x_column"], line["series_columns"]) == ("line", "month", ["revenue"])


def test_automatic_chart_and_unsuitable_or_invalid_configurations():
    automatic = build_visualization(classify_intent("Revenue by department").model_dump(), ["department", "revenue"], [{"department":"A","revenue":2}])
    assert automatic["type"] == "bar"
    too_many = [{"department":f"D{i}","revenue":i+1} for i in range(21)]
    unsuitable = build_visualization(classify_intent("Show a pie chart").model_dump(), ["department", "revenue"], too_many)
    assert unsuitable["type"] is None and "21 categories" in unsuitable["reason"] and unsuitable["alternative"]
    invalid = build_visualization(classify_intent("Show a line chart").model_dump(), ["department", "note"], [{"department":"A","note":"high"}])
    assert invalid["type"] is None and "numeric measure" in invalid["reason"]
    empty = build_visualization(classify_intent("Show a pie chart").model_dump(), ["department", "revenue"], [])
    assert empty["type"] is None and "No rows" in empty["reason"]
    missing = build_visualization(classify_intent("Show a pie chart").model_dump(), ["department", "revenue"], [{"department":"A","revenue":None}])
    assert missing["type"] is None and "numeric measure" in missing["reason"]
    partial = build_visualization(classify_intent("Show a pie chart").model_dump(), ["department", "revenue"], [{"department":"A","revenue":2},{"department":"B","revenue":None}])
    assert partial["type"] == "pie" and partial["omitted_rows"] == 1
    unsupported = build_visualization({"intent":"visualization", "requested_chart":"radar", "explicit_chart":True}, ["department", "revenue"], [{"department":"A","revenue":2}])
    assert unsupported["type"] is None and "not supported" in unsupported["reason"]


@pytest.mark.parametrize("suffix,content,table", [
    (".sqlite", "sqlite", "department_metrics"),
    (".csv", "csv", "metrics"),
    (".xlsx", "xlsx", "Department_Metrics"),
])
def test_dataset_summary_has_verified_statistics_for_sqlite_csv_and_xlsx(upload_settings, suffix, content, table):
    session = {".sqlite":"1"*32, ".csv":"2"*32, ".xlsx":"3"*32}[suffix]
    if content == "sqlite": payload = sqlite_bytes(upload_settings)
    elif content == "csv": payload = csv_bytes()
    else: payload = xlsx_bytes()
    register_upload(session, f"metrics{suffix}", payload, 1024 * 1024, 5)
    generated, verbalized = [], []

    def sql_model(*args):
        generated.append(args)
        raise AssertionError("dataset summary should profile the database directly")

    def summary_model(question, evidence):
        verbalized.append((question, evidence))
        return f"Summary verified for {table}: {evidence['tables'][table]['row_count']} rows."

    result = run_query("Summarize this dataset", model_fn=sql_model, summary_fn=summary_model, session_id=session)
    stats = result["statistics"]["tables"][table]
    assert result["status"] == "success"
    assert result["response_type"] == "summary"
    assert result["rows"] == []
    assert result["columns"] == []
    assert stats["row_count"] == 3
    assert stats["columns"]["employees"]["sum"] == 12
    assert stats["columns"]["employees"]["average"] == 4
    assert stats["columns"]["employees"]["minimum"] == 3
    assert stats["columns"]["employees"]["maximum"] == 5
    assert stats["columns"]["revenue"]["missing_count"] == 1
    assert stats["columns"]["revenue"]["sum"] == 160
    assert "Summary verified" in result["summary"]
    evidence_text = json.dumps(verbalized[0][1])
    assert "Avery" not in evidence_text and "rows_data" not in evidence_text
    assert generated == []


@pytest.mark.parametrize("provider_result", [None, "None", "null", ""])
def test_dataset_summary_uses_readable_profile_when_provider_returns_no_summary(upload_settings, provider_result):
    session = "4" * 32
    payload = b"department_id,department_name\n1,Sales\n2,Engineering\n3,Support\n4,HR\n5,Finance\n6,Legal\n"
    register_upload(session, "department.csv", payload, 1024 * 1024, 5)
    result = run_query("Summarize this file", model_fn=lambda *_: (_ for _ in ()).throw(AssertionError("summary must profile, not generate SQL")),
                       summary_fn=lambda *_: provider_result, session_id=session)
    assert result["status"] == "success"
    assert result["response_type"] == "summary"
    assert "department.csv" in result["summary"]
    assert "department_id" in result["summary"]
    assert "department_name" in result["summary"]
    assert "6 table rows" in result["summary"]


def test_dataset_summary_survives_provider_exception(upload_settings):
    session = "5" * 32
    payload = b"department_id,department_name\n1,Sales\n2,Engineering\n3,Support\n4,HR\n5,Finance\n6,Legal\n"
    register_upload(session, "department.csv", payload, 1024 * 1024, 5)
    def unavailable(*_):
        raise RuntimeError("provider unavailable")
    result = run_query("Summarize this file", summary_fn=unavailable, session_id=session)
    assert result["status"] == "success"
    assert "department.csv" in result["summary"]
    assert "department_name" in result["summary"]


def test_query_result_summary_falls_back_to_column_profile_when_provider_returns_none():
    result = run_query("Summarize the previous query result", summary_fn=lambda *_: "None",
                       previous_result={"columns":["department","employee_count"],
                                        "rows":[{"department":"Sales","employee_count":3},{"department":"HR","employee_count":2}]})
    assert result["status"] == "success"
    assert "2 rows and 2 columns" in result["summary"]
    assert "employee_count" in result["summary"]
    assert "average 2.5" in result["summary"]


def test_comparison_returns_verified_sql_rows_and_explanation(upload_settings):
    settings = get_settings()
    old_path = settings.database_path
    path = upload_settings / "compare.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE employees(name TEXT, department TEXT); INSERT INTO employees VALUES ('A','Engineering'),('B','Engineering'),('C','Sales');")
    connection.commit(); connection.close()
    settings.database_path = path
    try:
        result = run_query("Compare employee counts by department", lambda *args: ModelQuery(sql="SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department ORDER BY employee_count DESC", explanation="Compare employee counts."), summary_fn=lambda *_: "unused")
    finally:
        settings.database_path = old_path
    assert result["response_type"] == "comparison"
    assert result["rows"] == [{"department":"Engineering","employee_count":2},{"department":"Sales","employee_count":1}]
    assert result["visualization"]["type"] == "bar"
    assert result["summary"] == "Engineering is highest at 2, ahead of Sales by 1, based on the returned results."


def test_ordinary_query_still_executes_without_summary_provider(upload_settings):
    settings = get_settings()
    old_path = settings.database_path
    path = upload_settings / "query.sqlite"
    con = sqlite3.connect(path); con.execute("CREATE TABLE metrics(value INTEGER)"); con.executemany("INSERT INTO metrics VALUES (?)", [(2,), (3,)]); con.commit(); con.close()
    settings.database_path = path
    try:
        result = run_query("What is the total value?", lambda *args: ModelQuery(sql="SELECT SUM(value) AS total FROM metrics", explanation="Total."))
    finally:
        settings.database_path = old_path
    assert result["status"] == "success" and result["rows"] == [{"total":5}]
    assert result["response_type"] == "query"


def test_unsupported_schema_question_is_not_misreported_as_a_dataset_summary(upload_settings):
    settings = get_settings()
    old_path = settings.database_path
    path = upload_settings / "schema.sqlite"
    con = sqlite3.connect(path); con.execute("CREATE TABLE metrics(value INTEGER)"); con.commit(); con.close()
    settings.database_path = path
    try:
        result = run_query("What is the average salary?", lambda *args: ModelQuery(sql="SELECT 1 WHERE 0", explanation="Cannot be answered from the available schema."))
    finally:
        settings.database_path = old_path
    assert result["status"] == "refused"
    assert "cannot be answered" in result["error"]


def test_query_result_summary_uses_bounded_prior_result_from_session(upload_settings, monkeypatch):
    from app import main, workflow
    from app.schemas import QueryRequest
    from starlette.requests import Request
    from starlette.responses import Response

    settings = get_settings()
    old_path = settings.database_path
    path = upload_settings / "prior.sqlite"
    con = sqlite3.connect(path); con.execute("CREATE TABLE metrics(department TEXT, total INTEGER)"); con.executemany("INSERT INTO metrics VALUES (?,?)", [("Engineering",5),("Sales",2)]); con.commit(); con.close()
    settings.database_path = path
    session = "4" * 32
    main.query_results_by_session.pop(session, None)
    summary_calls = []

    def runner(question, session_id, database_id=None, previous_result=None):
        return workflow.run_query(
            question,
            model_fn=lambda *_: ModelQuery(sql="SELECT department, total FROM metrics ORDER BY total DESC", explanation="Query."),
            summary_fn=lambda q, stats: summary_calls.append(stats) or f"The prior result contained {stats['query_result']['row_count']} rows.",
            session_id=session_id,
            database_id=database_id,
            previous_result=previous_result,
        )

    def req():
        return Request({"type":"http","headers":[(b"x-querypilot-session",session.encode())],"method":"POST","path":"/api/query","query_string":b"","server":("test",80),"client":("test",1),"scheme":"http","http_version":"1.1"})

    monkeypatch.setattr(main, "run_query", runner)
    try:
        result = main.query(QueryRequest(question="Compare values by department"), Response(), req())
        assert result.rows == [{"department":"Engineering","total":5},{"department":"Sales","total":2}]
        summary = main.query(QueryRequest(question="Summarize the previous query result"), Response(), req())
        assert summary.response_type == "summary"
        assert summary.rows == []
        assert summary.summary == "The prior result contained 2 rows."
        assert summary.statistics["query_result"]["columns"]["total"]["sum"] == 7
        assert len(summary_calls) == 1
    finally:
        main.query_results_by_session.pop(session, None)
        settings.database_path = old_path


def test_query_result_summary_without_prior_result_explains_context_limit():
    result = run_query("Summarize the previous query result", lambda *_: (_ for _ in ()).throw(AssertionError("must not generate SQL")), summary_fn=lambda *_: (_ for _ in ()).throw(AssertionError("must not summarize without evidence")))
    assert result["status"] == "refused"
    assert "no previous query result" in result["error"].lower()
