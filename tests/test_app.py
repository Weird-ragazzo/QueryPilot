import sqlite3
from pathlib import Path
import pytest
from app.database import initialize_database
from app.guardrails import SQLRejected, validate_sql
from app.config import get_settings
from app import workflow
from app.schemas import ModelQuery
from app.schemas import QueryRequest


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "retail.sqlite3"
    initialize_database(path)
    settings = get_settings()
    monkeypatch.setattr(settings, "database_path", path)
    return path


def test_guardrails_allow_select_and_reject_unsafe():
    assert "SELECT" in validate_sql("select name from customers limit 3").upper()
    for sql in ["DELETE FROM customers", "DROP TABLE customers", "SELECT * FROM sqlite_master", "SELECT name FROM customers; SELECT 1", "SELECT secret FROM customers", "PRAGMA table_info(customers)"]:
        with pytest.raises(SQLRejected): validate_sql(sql)


def test_guardrails_validate_qualified_columns():
    with pytest.raises(SQLRejected): validate_sql("SELECT c.password FROM customers c")
    assert "customers" in validate_sql("SELECT x.name FROM customers x")


def test_database_is_read_only(db):
    before = db.read_bytes()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError): con.execute("DELETE FROM customers")
    con.close()
    assert db.read_bytes() == before


def test_row_limit(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_rows", 3)
    result = workflow.run_query("List customers", lambda *args: ModelQuery(sql="SELECT name FROM customers", explanation="List customers."))
    assert result["status"] == "error"
    assert "result limit" in result["error"]


def test_workflow_runs_generated_query_read_only(db):
    result = workflow.run_query("How many customers?", lambda *args: ModelQuery(sql="SELECT COUNT(*) AS count FROM customers", explanation="Count customers."))
    assert result["status"] == "success"
    assert result["rows"] == [{"count": 60}]
    assert "count = 60" in result["summary"]
    assert db.exists()


@pytest.mark.parametrize("sql", ["SELECT 1 WHERE 0", "SELECT 1 WHERE FALSE"])
def test_model_refusal_sentinel_is_not_reported_as_success(db, sql):
    result = workflow.run_query("Question outside the available schema", lambda *args: ModelQuery(sql=sql, explanation="Cannot be answered from the schema."))
    assert result["status"] == "refused"


def test_unsafe_model_sql_is_never_executed(db):
    before = db.read_bytes()
    result = workflow.run_query("do something", lambda *args: ModelQuery(sql="DROP TABLE customers", explanation="Drop."))
    assert result["status"] == "refused"
    assert result["error_code"] == "READ_ONLY_OPERATION_BLOCKED"
    assert result["error"] == "Request blocked: QueryPilot only supports read-only analytics. No database changes were made."
    assert db.read_bytes() == before


def test_single_correction_limit(db):
    calls = []
    def model(question, schema, error=None):
        calls.append(error)
        sql = "SELECT not_a_real_function(name) FROM customers" if len(calls) == 1 else "SELECT COUNT(*) AS count FROM customers"
        return ModelQuery(sql=sql, explanation="Count customers.")
    result = workflow.run_query("How many customers?", model)
    assert result["status"] == "success"
    assert result["attempts"] == 1
    assert len(calls) == 2


def test_failed_correction_is_not_repeated(db):
    calls = []
    def model(question, schema, error=None):
        calls.append(error)
        return ModelQuery(sql="SELECT missing_function(name) FROM customers", explanation="Count customers.")
    result = workflow.run_query("How many customers?", model)
    assert result["status"] == "error"
    assert result["attempts"] == 1
    assert len(calls) == 2


def test_health_and_schema_endpoints(db):
    from starlette.requests import Request
    from starlette.responses import Response
    from app.main import health, schema
    request = Request({"type":"http","headers":[(b"x-querypilot-session",b"1"*32)],"method":"GET","path":"/api/schema","query_string":b"","server":("test",80),"client":("test",1),"scheme":"http","http_version":"1.1"})
    assert health() == {"status": "ok"}
    result = schema(request, Response())
    assert set(result["tables"]) == {"customers", "products", "orders", "order_items"}
    assert result["tables"]["customers"]["columns"]["created_at"]["type"] == "TEXT"
    assert result["tables"]["customers"]["primary_keys"] == ["customer_id"]


def test_query_endpoint_preserves_response_and_handles_model_errors(monkeypatch):
    import asyncio
    from starlette.requests import Request
    from starlette.responses import Response
    from app import main
    monkeypatch.setattr(main, "run_query", lambda question, **kwargs: {"status": "error", "error": "The language model request timed out.", "attempts": 0})
    request = Request({"type":"http","headers":[],"method":"POST","path":"/api/query","query_string":b"","server":("test",80),"client":("test",1),"scheme":"http","http_version":"1.1"})
    response = Response()
    result = main.query(QueryRequest(question="Revenue total"), response, request)
    assert result.status == "error"
    assert result.error == result.summary
    assert result.sql is None
    assert response.status_code == 503


def test_question_length_validation():
    with pytest.raises(Exception): QueryRequest(question="ab")
    with pytest.raises(Exception): QueryRequest(question="x" * 1001)


@pytest.mark.parametrize("question", [
    "delete all departments",
    "update every employee's role",
    "drop the department table",
])
def test_destructive_questions_are_blocked_before_generation_or_database_execution(db, monkeypatch, question):
    from app import main
    from starlette.responses import Response

    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE departments(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO departments VALUES(1, 'Engineering'); CREATE TABLE employees(id INTEGER PRIMARY KEY, role TEXT); INSERT INTO employees VALUES(1, 'Analyst');")
    con.commit(); con.close()
    original = db.read_bytes()
    model_calls, database_opens = [], []

    def model(*args, **kwargs):
        model_calls.append(args)
        return ModelQuery(sql="SELECT 1", explanation="should not run")

    def open_database(*args, **kwargs):
        database_opens.append(args)
        raise AssertionError("database execution must not be reached")

    monkeypatch.setattr(main, "run_query", lambda text, **kwargs: workflow.run_query(text, model_fn=model, **kwargs))
    monkeypatch.setattr(workflow, "open_readonly", open_database)
    from starlette.requests import Request
    http_request = Request({"type":"http","headers":[(b"x-querypilot-session",b"a"*32)],"method":"POST","path":"/api/query","query_string":b"","server":("test",80),"client":("test",1),"scheme":"http","http_version":"1.1"})
    http_response = Response()
    result = main.query(QueryRequest(question=question), http_response, http_request)
    body = result.model_dump()
    assert http_response.status_code == 403
    assert body["status"] == "refused"
    assert body["error_code"] == "READ_ONLY_OPERATION_BLOCKED"
    assert body["error"] == "Request blocked: QueryPilot only supports read-only analytics. No database changes were made."
    assert body["summary"] == body["error"]
    assert model_calls == []
    assert database_opens == []
    assert db.read_bytes() == original


def test_malformed_query_payload_keeps_pydantic_validation(db):
    with pytest.raises(Exception):
        QueryRequest(question="ab")


def workflow_app():
    from app.main import app
    return app

