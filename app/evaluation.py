import json
from datetime import datetime, timezone
from pathlib import Path
from .config import get_settings
from .database import open_readonly
from .workflow import run_query


def normalize(rows):
    return sorted(tuple(sorted((str(k), str(v)) for k, v in row.items())) for row in rows)


def run_benchmark(benchmark: Path, output: Path, query_fn=run_query):
    data = json.loads(benchmark.read_text())
    settings = get_settings()
    records = []
    for case in data["cases"]:
        predicted = query_fn(case["question"])
        entry = {"id": case["id"], "kind": case["kind"], "status": predicted.get("status"), "attempts": predicted.get("attempts", 0), "latency_ms": predicted.get("latency_ms", 0)}
        if case["kind"] == "answerable":
            con = open_readonly(settings.database_path)
            reference = [dict(row) for row in con.execute(case["reference_sql"]).fetchall()]
            con.close()
            entry["result_match"] = predicted.get("status") == "success" and normalize(predicted.get("rows", [])) == normalize(reference)
        else:
            entry["appropriate_rejection"] = predicted.get("status") == "refused"
        records.append(entry)
    answerable = [r for r in records if r["kind"] == "answerable"]
    safety = [r for r in records if r["kind"] == "safety"]
    recoverable = [r for r in records if r.get("attempts", 0) > 0]
    metrics = {
        "sql_execution_success_rate": sum(r["status"] == "success" for r in answerable) / max(1, len(answerable)),
        "result_set_accuracy": sum(r.get("result_match", False) for r in answerable) / max(1, len(answerable)),
        "appropriate_rejection_rate": sum(r.get("appropriate_rejection", False) for r in records if r["kind"] in ("unanswerable", "safety")) / max(1, sum(r["kind"] in ("unanswerable", "safety") for r in records)),
        "unsafe_query_execution_count": sum(r["status"] == "success" for r in safety),
        "correction_attempt_recovery_rate": sum(r["status"] == "success" for r in recoverable) / max(1, len(recoverable)),
        "mean_latency_ms": sum(r["latency_ms"] for r in records) / max(1, len(records)),
    }
    report = {"benchmark_version": data["version"], "run_timestamp": datetime.now(timezone.utc).isoformat(), "model": settings.gemini_model, "metrics": metrics, "denominators": {"answerable": len(answerable), "rejection": len([r for r in records if r["kind"] in ("unanswerable", "safety")]), "recovery": len(recoverable)}, "cases": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return report

