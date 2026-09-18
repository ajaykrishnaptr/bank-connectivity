"""
Categoriser experiments in Langfuse, from the command line.

  sync-benchmark   create or update fintnet-categorisation-benchmark
                   (300 stratified synthetic transactions; fixed seed)
  run-benchmark    one experiment run on the benchmark, e.g. after a prompt or model change
  run-daily        the same job the /cron/evaluate route runs, for a date

Run:
  .venv/bin/python evals/categoriser_experiment.py sync-benchmark
  .venv/bin/python evals/categoriser_experiment.py run-benchmark [--min-accuracy 0.85]
  .venv/bin/python evals/categoriser_experiment.py run-daily --date 2026-09-14 --size 150

Cost: one Claude Haiku call per item (300 for the benchmark, about $0.25).
Traces go to the "eval" environment; results are also saved to evals/results/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "eval")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from fintnet import app as appmod  # noqa: E402
from fintnet.ai import categorize as cat  # noqa: E402
from fintnet.ai import evaluate  # noqa: E402
from fintnet.ai import llm  # noqa: E402
from fintnet.telemetry import observability  # noqa: E402


def _save(kind: str, summary: dict) -> None:
    out = ROOT / "evals" / "results" / f"categoriser_{kind}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**summary, "llmUsage": llm.usage()}, indent=2, default=str))
    print(json.dumps({k: summary.get(k) for k in ("dataset", "run_name", "dataset_run_url", "model_accuracy",
                                                  "rules_accuracy", "model_failures")}, indent=2, default=str))
    print("saved", out.relative_to(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-benchmark")
    rb = sub.add_parser("run-benchmark")
    rb.add_argument("--min-accuracy", type=float)
    rd = sub.add_parser("run-daily")
    rd.add_argument("--date", default=str(date.today()))
    rd.add_argument("--size", type=int, default=150)
    args = ap.parse_args()

    lf = observability.client()
    with appmod.app.app_context():
        if args.cmd == "sync-benchmark":
            if lf is None:
                sys.exit("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY first")
            items = evaluate.benchmark_items(300)
            evaluate.sync_dataset(lf, evaluate.BENCHMARK, items, "300 stratified synthetic transactions, fixed seed")
            lf.flush()
            print(f"synced {evaluate.BENCHMARK} ({len(items)} items)")
        elif args.cmd == "run-benchmark":
            if not llm.available():
                sys.exit("no ANTHROPIC_API_KEY")
            items = evaluate.benchmark_items(300)
            ver = observability.version(cat.SYSTEM)
            summary = evaluate.experiment(evaluate.BENCHMARK, items,
                                          run_name=f"benchmark · {llm.MODEL} · prompt {ver} · "
                                                   f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
                                          description="Benchmark run")
            _save("benchmark", summary)
            acc = summary["model_accuracy"].get("all") or 0
            if args.min_accuracy is not None and acc < args.min_accuracy:
                sys.exit(f"accuracy {acc} below {args.min_accuracy}")
        else:
            _save("daily", evaluate.run_daily(date.fromisoformat(args.date), size=args.size))
        observability.flush()


if __name__ == "__main__":
    main()
