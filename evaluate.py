"""
Categoriser evaluation as Langfuse datasets and experiment runs.

This is the only module that reads SbLabel (label firewall). Labels leave the
database only as Langfuse dataset expected outputs, which no model call reads.

Two datasets:
  * fintnet-categorisation-daily/<booking date>: the daily cron job samples
    that day's evaluation-population transactions (stratified: new merchant,
    hard case, seen merchant), creates the dataset and runs one experiment.
  * fintnet-categorisation-benchmark: a fixed stratified sample for comparing
    models and prompt versions side by side (evals/categoriser_experiment.py).

Each experiment item runs the model with overrides and cache bypassed, so it
measures the model, not what the cache remembers. Evaluators, all in code:
  * item: category_correct (model), rules_correct (keyword baseline)
  * run: accuracy, rules_accuracy, accuracy per slice, calibration per
    confidence bucket, model failures

Without Langfuse keys the same task and evaluators run locally.
"""
from __future__ import annotations

import hashlib
import os
import random
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

import categorize as cat
import llm
import observability
from models import SbLabel, SbTransaction, db

DAILY_PREFIX = "fintnet-categorisation-daily"
BENCHMARK = "fintnet-categorisation-benchmark"
_BUCKETS = [(0, 59), (60, 79), (80, 89), (90, 100)]


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


# ── Sampling (reads labels) ──────────────────────────────────────────────────

def latest_day_with_data(day: date) -> date | None:
    return db.session.scalar(select(func.max(SbTransaction.booking_date)).where(SbTransaction.booking_date <= day))


def _rows(where) -> list[tuple[SbTransaction, SbLabel]]:
    return db.session.execute(
        select(SbTransaction, SbLabel).join(SbLabel, SbLabel.transaction_id == SbTransaction.transaction_id)
        .where(SbTransaction.customer_id.like("POP-%"), where)).all()


def _stratified(rows, size: int, seed: str) -> list[tuple[SbTransaction, SbLabel]]:
    by_slice: dict[str, list] = defaultdict(list)
    for t, lab in rows:
        by_slice[lab.slice].append((t, lab))
    rng = random.Random(seed)
    picked: list = []
    per = max(1, size // 3)
    for slice_ in ("novel", "hard", "seen"):
        pool = sorted(by_slice.get(slice_, []), key=lambda r: r[0].transaction_id)
        rng.shuffle(pool)
        picked.extend(pool[:per])
    chosen = {t.transaction_id for t, _ in picked}
    rest = sorted((r for r in rows if r[0].transaction_id not in chosen), key=lambda r: r[0].transaction_id)
    rng.shuffle(rest)
    return picked + rest[: max(0, size - len(picked))]


def to_item(t: SbTransaction, lab: SbLabel) -> dict:
    return {"id": f"tx-{t.transaction_id}",
            "input": {"merchant": t.creditor_name or t.debtor_name or "", "remittance": t.remittance or "",
                      "amount": float(t.amount), "currency": t.currency},
            "expected_output": {"category": lab.category},
            "metadata": {"slice": lab.slice, "booking_date": t.booking_date.isoformat(),
                         "country": t.customer_id.split("-")[1], "merchant_truth": lab.merchant}}


def daily_items(day: date, size: int) -> tuple[date | None, list[dict]]:
    target = latest_day_with_data(day)
    if target is None:
        return None, []
    rows = _rows(SbTransaction.booking_date == target)
    return target, [to_item(t, lab) for t, lab in _stratified(rows, size, f"daily|{target}")]


def benchmark_items(size: int = 300) -> list[dict]:
    newest = latest_day_with_data(date.today())
    if newest is None:
        return []
    rows = _rows(SbTransaction.booking_date <= newest)
    return [to_item(t, lab) for t, lab in _stratified(rows, size, "benchmark-v1")]


# ── Task and evaluators ──────────────────────────────────────────────────────

# Which kind of hard case a merchant string is, so the run says what actually hurts.
_FACILITATOR = ("SQ *", "SP ", "PAYPAL *", "SUMUP *", "IZ *", "ZTL*", "PP*")


def _hard_case(meta: dict | None) -> str:
    merchant = ((meta or {}).get("merchant_truth") or "")
    if any(merchant.upper().startswith(p) for p in _FACILITATOR):
        return "hard:facilitator"
    if merchant.isupper() and len(merchant) <= 14:
        return "hard:truncated"
    return "hard:typo_or_misleading"


def _margin(pair: list[int]) -> float | None:
    """Half-width of the 95% interval around an accuracy, given the sample."""
    correct, total = pair
    if not total:
        return None
    p = correct / total
    return round(1.96 * ((p * (1 - p) / total) ** 0.5), 3)


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def task(*, item, **_) -> dict:
    """Three answers per item, so the run compares what ships with what the model alone does.

      shipped  what production returns at insert time: overrides, then the cache,
               then rules (`categorize_detailed()`), the category a customer sees
               until the nightly job upgrades it
      model    the model on its own, uncached
      rules    the keyword baseline
    """
    x = _field(item, "input")
    rules = cat._categorize_by_rules(x["merchant"])
    shipped, shipped_source = cat.categorize_detailed(x["merchant"])
    try:
        answer = cat.model_categorize(x["merchant"], x.get("remittance", ""), x.get("amount"),
                                      name="evaluate-categorise")
        return {"category": answer["category"], "confidence": answer["confidence"], "reason": answer["reason"],
                "rules": rules, "shipped": shipped, "shipped_source": shipped_source, "mode": "model"}
    except llm.LLMUnavailable as exc:
        return {"category": None, "confidence": None, "reason": str(exc)[:200], "rules": rules,
                "shipped": shipped, "shipped_source": shipped_source, "mode": "failed"}


def item_evaluator(*, output, expected_output, **_):
    from langfuse import Evaluation

    truth = expected_output["category"]
    return [Evaluation(name="category_correct", value=1.0 if output["category"] == truth else 0.0,
                       comment=f"expected {truth}, got {output['category']} ({output['confidence']})"),
            Evaluation(name="shipped_correct", value=1.0 if output.get("shipped") == truth else 0.0,
                       comment=f"via {output.get('shipped_source')}"),
            Evaluation(name="rules_correct", value=1.0 if output["rules"] == truth else 0.0)]


def summarise(pairs: list[tuple[dict, dict, dict]]) -> dict:
    """pairs of (output, expected_output, metadata) -> accuracy figures."""
    model, rules = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    shipped = defaultdict(lambda: [0, 0])
    by_category, by_case, confusions = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(int)
    calib = {b: [0, 0] for b in _BUCKETS}
    failures, errors = 0, []
    for out, exp, meta in pairs:
        truth, slice_ = exp["category"], (meta or {}).get("slice", "unknown")
        for key in ("all", slice_):
            rules[key][0] += int(out["rules"] == truth)
            rules[key][1] += 1
            shipped[key][0] += int(out.get("shipped") == truth)
            shipped[key][1] += 1
        if out["mode"] != "model":
            failures += 1
            continue
        ok = out["category"] == truth
        for key in ("all", slice_):
            model[key][0] += int(ok)
            model[key][1] += 1
        by_category[truth][0] += int(ok)
        by_category[truth][1] += 1
        case = _hard_case(meta) if slice_ == "hard" else slice_
        by_case[case][0] += int(ok)
        by_case[case][1] += 1
        if not ok:
            confusions[f"{truth} -> {out['category']}"] += 1
        bucket = next(b for b in _BUCKETS if b[0] <= out["confidence"] <= b[1])
        calib[bucket][0] += int(ok)
        calib[bucket][1] += 1
        if not ok and len(errors) < 15:
            errors.append({"merchant": (meta or {}).get("merchant_truth"), "slice": slice_, "expected": truth,
                           "got": out["category"], "confidence": out["confidence"]})

    def acc(p):
        return round(p[0] / p[1], 3) if p[1] else None

    n_all = rules["all"][1]
    return {"model_accuracy": {k: acc(v) for k, v in model.items()},
            "shipped_accuracy": {k: acc(v) for k, v in shipped.items()},
            "rules_accuracy": {k: acc(v) for k, v in rules.items()},
            "n": {k: v[1] for k, v in rules.items()},
            # ±1.96 standard errors on the headline figure, so a normal wobble is not read as drift
            "model_accuracy_margin": _margin(model["all"]),
            "by_category": {k: {"accuracy": acc(v), "n": v[1]} for k, v in sorted(by_category.items())},
            "by_case": {k: {"accuracy": acc(v), "n": v[1]} for k, v in sorted(by_case.items())},
            "top_confusions": dict(sorted(confusions.items(), key=lambda kv: -kv[1])[:8]),
            "calibration": {f"{lo}-{hi}": {"accuracy": acc(v), "n": v[1]} for (lo, hi), v in calib.items()},
            "model_failures": failures, "errors": errors, "sample_size": n_all}


def run_evaluator(*, item_results, **_):
    from langfuse import Evaluation

    s = summarise([(r.output, _field(r.item, "expected_output"), _field(r.item, "metadata")) for r in item_results])
    evals = [Evaluation(name="model_failures", value=float(s["model_failures"]))]
    for engine in ("model", "shipped", "rules"):
        for key, value in s[f"{engine}_accuracy"].items():
            if value is not None:
                prefix = {"model": "accuracy", "shipped": "shipped_accuracy", "rules": "rules_accuracy"}[engine]
                name = prefix + ("" if key == "all" else f"_{key}")
                evals.append(Evaluation(name=name, value=value, comment=f"n={s['n'].get(key)}"))
    for bucket, v in s["calibration"].items():
        if v["accuracy"] is not None:
            evals.append(Evaluation(name=f"calibration_{bucket}", value=v["accuracy"], comment=f"n={v['n']}"))
    return evals


# ── Running ──────────────────────────────────────────────────────────────────

def item_id(dataset: str, base_id: str) -> str:
    """Langfuse item ids are unique per project across datasets, so scope them to the dataset."""
    return hashlib.sha1(f"{dataset}|{base_id}".encode()).hexdigest()[:32]


def sync_dataset(lf, name: str, items: list[dict], description: str) -> None:
    lf.create_dataset(name=name, description=description,
                      metadata={"source": "synthbank sb_labels", "label_firewall": "evaluation only"})
    for it in items:
        lf.create_dataset_item(dataset_name=name, id=item_id(name, it["id"]), input=it["input"],
                               expected_output=it["expected_output"],
                               metadata={**it["metadata"], "transaction": it["id"]})


def experiment(dataset: str, items: list[dict], run_name: str, description: str) -> dict:
    """Create or update the dataset, run one experiment on it, return the summary."""
    meta = {"model": llm.MODEL, "promptVersion": observability.version(cat.SYSTEM), "commit": _commit()}
    lf = observability.client()
    if lf is None:  # no Langfuse keys: same task and evaluators, locally
        outputs = [(task(item=it), it["expected_output"], it["metadata"]) for it in items]
        return {"langfuse": False, "dataset": dataset, "run_name": run_name, **meta, **summarise(outputs)}
    sync_dataset(lf, dataset, items, description)
    lf.flush()
    result = lf.get_dataset(dataset).run_experiment(
        name="fintnet categorisation", run_name=run_name, description=description, task=task,
        evaluators=[item_evaluator], run_evaluators=[run_evaluator], max_concurrency=3, metadata=meta)
    lf.flush()
    summary = summarise([(r.output, _field(r.item, "expected_output"), _field(r.item, "metadata"))
                         for r in result.item_results])
    return {"langfuse": True, "dataset": dataset, "run_name": run_name, "dataset_run_id": result.dataset_run_id,
            "dataset_run_url": result.dataset_run_url, **meta, **summary}


def run_daily(day: date, size: int = 150) -> dict:
    target, items = daily_items(day, size)
    if not items:
        return {"status": "warn", "error": "no synthetic transactions to evaluate"}
    ver = observability.version(cat.SYSTEM)
    summary = experiment(f"{DAILY_PREFIX}/{target.isoformat()}", items,
                         run_name=f"daily {target.isoformat()} · {llm.MODEL} · prompt {ver}",
                         description=f"Stratified sample of {len(items)} synthetic transactions booked {target}")
    summary["booking_date"] = target.isoformat()
    summary["sample_size"] = len(items)
    summary.update(gate(summary))
    return summary


def gate(summary: dict) -> dict:
    """Turn the run into a pass or a fail, so a bad day is not reported as ok.

    error  the model could not answer, or it fell below EVAL_MIN_ACCURACY (0.85)
    warn   it no longer beats the keyword rules by EVAL_MIN_MARGIN (0.05), or too
           many calls failed
    """
    accuracy = (summary.get("model_accuracy") or {}).get("all")
    rules = (summary.get("rules_accuracy") or {}).get("all") or 0.0
    floor = float(os.getenv("EVAL_MIN_ACCURACY", "0.85"))
    margin = float(os.getenv("EVAL_MIN_MARGIN", "0.05"))
    failures = summary.get("model_failures", 0)
    sample = summary.get("sample_size") or 0
    if accuracy is None:
        return {"status": "error", "gate": "the model returned nothing to score"}
    if accuracy < floor:
        return {"status": "error", "gate": f"accuracy {accuracy:.3f} below the floor {floor:.2f}"}
    if accuracy - rules < margin:
        return {"status": "warn", "gate": f"accuracy {accuracy:.3f} is within {margin:.2f} of the rules {rules:.3f}"}
    if sample and failures / sample > 0.05:
        return {"status": "warn", "gate": f"{failures} of {sample} model calls failed"}
    return {"status": "ok", "gate": f"accuracy {accuracy:.3f} ± {summary.get('model_accuracy_margin')} "
                                    f"beats rules {rules:.3f}"}
