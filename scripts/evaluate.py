import argparse
from pathlib import Path
from app.evaluation import run_benchmark

parser = argparse.ArgumentParser()
parser.add_argument("--benchmark", type=Path, default=Path("benchmarks/v1.json"))
parser.add_argument("--output", type=Path, default=Path("reports/latest.json"))
args = parser.parse_args()
print(run_benchmark(args.benchmark, args.output)["metrics"])

