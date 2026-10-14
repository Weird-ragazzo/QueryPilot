import io
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app import main
from app.config import get_settings
from app.conversations import append_turn, create_conversation, get_conversation
from app.data_sources import lock_sources
from app.schemas import ModelQuery, QueryRequest
from app.workflow import run_query


def request_for(session, method="GET", path="/api/workspace"):
    return Request({"type":"http","headers":[(b"x-querypilot-session",session.encode())],"method":method,"path":path,"query_string":b"","server":("test",80),"client":("test",1),"scheme":"http","http_version":"1.1"})


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    get_settings().max_upload_bytes = 1024 * 1024
    session = "f" * 32
    made = []
    for name, department in (("one", "Sales"), ("two", "HR")):
        path = tmp_path / f"{name}.sqlite"
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE employee(id INTEGER, department TEXT); INSERT INTO employee VALUES (1, '" + department + "');")
        con.commit(); con.close()
        made.append(main.register_upload(session, path.name, path.read_bytes(), 1024 * 1024, 5))
    return tmp_path, session, made


def test_sources_can_be_deleted_but_sample_is_protected(workspace):
    root, session, sources = workspace
    body = main.list_databases(session)
    assert len(body["databases"]) == 3
    path = main.resolve_database(session, sources[0]["id"]).path
    removed = main.delete_source(sources[0]["id"], request_for(session, "DELETE"), Response())
    assert not path.exists()
    assert [db["id"] for db in removed["databases"]] == ["sample", sources[1]["id"]]
    with pytest.raises(HTTPException) as error:
        main.delete_source("sample", request_for(session, "DELETE"), Response())
    assert error.value.status_code == 400


def test_deleting_active_upload_leaves_other_upload_available_for_deletion(workspace):
    _, session, sources = workspace
    # The newest upload is active. Removing it must not hide the older source.
    remaining = main.delete_source(sources[1]["id"], request_for(session, "DELETE"), Response())
    assert [db["id"] for db in remaining["databases"]] == ["sample", sources[0]["id"]]
    final = main.delete_source(sources[0]["id"], request_for(session, "DELETE"), Response())
    assert [db["id"] for db in final["databases"]] == ["sample"]


def test_delete_cors_preflight_allows_browser_request_method():
    cors = next(middleware for middleware in main.app.user_middleware if middleware.cls.__name__ == "CORSMiddleware")
    assert "DELETE" in cors.kwargs["allow_methods"]


def test_source_deletion_waits_for_active_query_lease(workspace):
    _, session, sources = workspace
    entered, release, deleted = Event(), Event(), Event()
    def query_lease():
        with lock_sources(session, [sources[0]["id"]]):
            entered.set(); release.wait(2)
    def delete():
        entered.wait(2)
        main.delete_database(session, sources[0]["id"])
        deleted.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        query = pool.submit(query_lease); removal = pool.submit(delete)
        assert entered.wait(1)
        time.sleep(.03)
        assert not deleted.is_set()
        release.set(); query.result(2); removal.result(2)
    assert deleted.is_set()


def test_conversations_are_session_owned_and_resettable():
    first = create_conversation("a" * 32)
    second = create_conversation("b" * 32)
    append_turn("a" * 32, first["conversation_id"], {"role":"user","content":"count"}, {"role":"assistant","content":"one"})
    assert len(get_conversation("a" * 32, first["conversation_id"])["messages"]) == 2
    with pytest.raises(KeyError):
        get_conversation("b" * 32, first["conversation_id"])
    cleared = main.reset_conversation("a" * 32, first["conversation_id"])
    assert cleared["messages"] == []
    assert get_conversation("b" * 32, second["conversation_id"])["messages"] == []


def test_multi_source_summaries_are_separate_and_cross_source_sql_is_not_run(workspace, monkeypatch):
    _, session, sources = workspace
    called = []
    monkeypatch.setattr(main, "run_query", lambda question, **kwargs: called.append(kwargs["database_id"]) or {"status":"success","summary":f"Summary for {kwargs['database_id']}","response_type":"summary","statistics":{"tables":{}},"columns":[],"rows":[],"duration_ms":1,"attempts":0})
    request = QueryRequest(question="Summarize both datasets", database_ids=[item["id"] for item in sources])
    result = main.query(request, Response(), request_for(session, "POST", "/api/query"))
    assert result.status == "success"
    assert called == [item["id"] for item in sources]
    assert all(item["name"] in result.summary for item in main.list_databases(session)["databases"] if item["id"] != "sample")
    called.clear()
    comparison = main.query(QueryRequest(question="Compare employee counts", database_ids=[item["id"] for item in sources]), Response(), request_for(session, "POST", "/api/query"))
    assert comparison.status == "refused"
    assert comparison.error_code == "CROSS_SOURCE_QUERY_UNSUPPORTED"
    assert called == []


def test_schema_endpoint_uses_each_source_and_csv_pdf_exports_are_session_scoped(workspace):
    _, session, sources = workspace
    ref = main.resolve_database(session, sources[0]["id"])
    discovered = main.source_schema(sources[0]["id"], request_for(session), Response())
    assert discovered["schema"]["tables"]["employee"]["columns"]["department"]["type"] == "TEXT"
    conversation = create_conversation(session)["conversation_id"]
    rid = "9" * 32
    append_turn(session, conversation, {"role":"user","content":"show rows"}, {"role":"assistant","content":"done","database_ids":[sources[0]["id"]],"database_names":[sources[0]["name"]],"result_id":rid,"result":{"question":"show rows","summary":"done","columns":["department"],"rows":[{"department":"=1+1"}]}})
    csv_response = main.export_csv(conversation, rid, request_for(session), Response())
    csv_text = csv_response.body.decode("utf-8-sig")
    assert csv_text == "department\r\n'=1+1\r\n"
    pdf_response = main.export_pdf(conversation, rid, request_for(session), Response())
    pdf = pdf_response.body
    assert pdf_response.media_type == "application/pdf"
    assert pdf.startswith(b"%PDF-")
    with pytest.raises(HTTPException) as error:
        main.export_csv(conversation, rid, request_for("c" * 32), Response())
    assert error.value.status_code == 404


def test_examine_data_returns_exact_duplicate_and_missing_statistics(workspace):
    _, session, sources = workspace
    path = main.resolve_database(session, sources[0]["id"]).path
    con = sqlite3.connect(path)
    con.execute("INSERT INTO employee VALUES (2, NULL)")
    con.execute("INSERT INTO employee VALUES (2, NULL)")
    con.commit(); con.close()
    result = run_query("Examine data quality", lambda *_: (_ for _ in ()).throw(AssertionError("examine intent must not generate SQL")),
                       session_id=session, database_id=sources[0]["id"], summary_fn=lambda *_: (_ for _ in ()).throw(AssertionError("examine must not call the LLM")))
    assert result["status"] == "success"
    assert result["response_type"] == "examine"
    assert "3 rows" in result["summary"]
    assert "2 missing values across 1 column" in result["summary"]
    assert "1 duplicate row" in result["summary"]
    profile = result["statistics"]["tables"]["employee"]
    assert profile["row_count"] == 3
    assert profile["duplicate_row_count"] == 1
    assert profile["columns"]["department"]["missing_count"] == 2
    assert profile["columns"]["department"]["distinct_count"] == 1
    assert result["statistics"]["overview"] == {"table_count":1,"view_count":0,"row_count":3,"view_row_count":0,"column_count":2,
        "missing_value_count":2,"columns_with_missing":1,"duplicate_row_count":1}
    assert result["statistics"]["timings_ms"]["profiling"] >= 0
    response=main.query(QueryRequest(question="Examine data quality",database_ids=[sources[0]["id"]]),Response(),request_for(session,"POST","/api/query"))
    assert response.response_type == "examine"
    assert response.summary and response.summary != "None"
    assert response.statistics["overview"]["missing_value_count"] == 2


def test_examine_empty_dataset_and_profile_failure_are_handled(workspace, monkeypatch):
    _, session, sources = workspace
    ref = main.resolve_database(session, sources[0]["id"])
    con=sqlite3.connect(ref.path); con.execute("DELETE FROM employee"); con.commit(); con.close()
    empty=run_query("Examine data", session_id=session, database_id=sources[0]["id"])
    assert empty["status"] == "success"
    assert "0 rows" in empty["summary"]
    assert empty["statistics"]["overview"]["missing_value_count"] == 0

    from app import workflow
    monkeypatch.setattr(workflow, "profile_database", lambda *_: (_ for _ in ()).throw(RuntimeError("internal db detail")))
    failed=run_query("Examine data", session_id=session, database_id=sources[0]["id"])
    assert failed["status"] == "error"
    assert "internal db detail" not in failed["error"]


def test_followup_uses_prior_context_and_chart_change_reuses_result(workspace, monkeypatch):
    _, session, _ = workspace
    calls = []
    def fake_run(question, **kwargs):
        calls.append(question)
        if len(calls) == 1:
            return {"status":"success","summary":"HR has 2 employees.","sql":"SELECT department, COUNT(*) AS total FROM employee GROUP BY department", "columns":["department","total"],"rows":[{"department":"HR","total":2},{"department":"Sales","total":1}],"duration_ms":2,"attempts":0,"response_type":"comparison"}
        return {"status":"success","summary":"Filtered.","sql":"SELECT department, COUNT(*) AS total FROM employee WHERE department='HR' GROUP BY department", "columns":["department","total"],"rows":[{"department":"HR","total":2}],"duration_ms":2,"attempts":0}
    monkeypatch.setattr(main, "run_query", fake_run)
    first = main.query(QueryRequest(question="Compare employee counts by department"), Response(), request_for(session, "POST", "/api/query"))
    second = main.query(QueryRequest(question="Filter it to HR", conversation_id=first.conversation_id), Response(), request_for(session, "POST", "/api/query"))
    assert "prior_question" in calls[1] and "result_columns" in calls[1] and "prior_sql" in calls[1]
    chart = main.query(QueryRequest(question="Show that as a pie chart", conversation_id=first.conversation_id), Response(), request_for(session, "POST", "/api/query"))
    assert chart.response_type == "visualization"
    assert chart.visualization["type"] == "pie"
    assert chart.rows == second.rows
    assert len(calls) == 2
