"""CLI entry point for running QueryPilot evaluation benchmarks."""
from __future__ import annotations

import argparse
from pathlib import Path
from evaluation.runner import run_evaluation


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.run",
        description="QueryPilot text-to-SQL reproducible evaluation and benchmarking framework.",
    )
    parser.add_argument(
        "--mode",
        choices=["offline", "live"],
        default="offline",
        help="Evaluation execution mode: 'offline' (deterministic mocks, zero network) or 'live' (active provider chain). Default is 'offline'.",
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=Path("evaluation/benchmark_cases.json"),
        help="Path to benchmark cases JSON file. Default is 'evaluation/benchmark_cases.json'.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("evaluation/reports"),
        help="Directory to save JSON, CSV, and Markdown evaluation reports. Default is 'evaluation/reports'.",
    )
    parser.add_argument(
        "--cases",
        type=str,
        default=None,
        help="Comma-separated list of case IDs to evaluate.",
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Filter evaluation to a specific category.",
    )
    parser.add_argument(
        "--difficulty",
        type=str,
        choices=["easy", "medium", "hard"],
        default=None,
        help="Filter evaluation to a specific difficulty.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Repeat evaluation N times for latency stability and consistency testing. Default is 1.",
    )
    parser.add_argument(
        "--limit",
        "--max-requests",
        dest="max_requests",
        type=int,
        default=None,
        help="Limit evaluation to the first N cases to preserve API quota.",
    )

    args = parser.parse_args()

    subset_ids = [c.strip() for c in args.cases.split(",")] if args.cases else None

    print(f"\n🚀 Running QueryPilot evaluation [{args.mode.upper()} MODE]...")
    print(f"   Benchmark file: {args.benchmark}")
    print(f"   Output dir:     {args.output_dir}")
    if subset_ids:
        print(f"   Case filter:    {subset_ids}")
    if args.category:
        print(f"   Category:       {args.category}")
    if args.max_requests:
        print(f"   Request limit:  {args.max_requests}")

    report = run_evaluation(
        benchmark_file=args.benchmark,
        output_dir=args.output_dir,
        mode=args.mode,
        subset_ids=subset_ids,
        subset_category=args.category,
        subset_difficulty=args.difficulty,
        repeat=args.repeat,
        max_requests=args.max_requests,
    )

    metrics = report["aggregate_metrics"]
    corr = metrics.get("correctness_and_reliability", {})
    sec = metrics.get("security_and_guardrails", {})
    lat = metrics.get("latency", {}).get("end_to_end", {})

    print("\n✅ Evaluation completed successfully!")
    print(f"   Total Cases Evaluated:       {report['benchmark_metadata']['total_evaluated_cases']}")
    print(f"   Exact Result Accuracy:       {corr.get('exact_result_accuracy', {}).get('value', 0) * 100:.1f}%")
    print(f"   Execution Success Rate:      {corr.get('sql_execution_success_rate', {}).get('value', 0) * 100:.1f}%")
    print(f"   Appropriate Refusal Rate:    {corr.get('correct_refusal_rate', {}).get('value', 0) * 100:.1f}%")
    print(f"   Forbidden Operations:        {sec.get('forbidden_execution_count', 0)} (Violations: {sec.get('security_violation_rate', 0) * 100:.1f}%)")
    print(f"   p50 Median Latency:          {lat.get('p50_ms', 'N/A')} ms (p95: {lat.get('p95_ms', 'N/A')} ms)")
    print(f"\n📄 Reports saved to:")
    print(f"   JSON: {args.output_dir / 'latest.json'}")
    print(f"   CSV:  {args.output_dir / 'latest.csv'}")
    print(f"   MD:   {args.output_dir / 'latest.md'}\n")


if __name__ == "__main__":
    main()
