"""
Daily jobs, triggered by Vercel Cron (vercel.json) or by hand.

  /cron/feed        book yesterday in the synthetic bank, prune to 13 months,
                    re-sync connected FintNet users
  /cron/categorise  upgrade provisional rule categories with the model (capped)
  /cron/evaluate    daily categoriser experiment in Langfuse (dataset + run)
  /cron/health      CRL, OCSP and certificate expiry checks

Auth: Vercel sends `Authorization: Bearer $CRON_SECRET`. Without CRON_SECRET
the routes only run outside Vercel (local development).

Idempotent: one JobRun row per job per date. A finished run (ok or warn) is
not repeated unless `?force=1`; a run started under 15 minutes ago is treated
as in progress. `?date=YYYY-MM-DD` books or evaluates a specific day.

Vercel Hobby runs each cron at most once a day, anywhere within the scheduled
hour, with a 300 second function limit; every job is sized to fit that.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request

from fintnet.ai import categorize as cat
from fintnet.ai import evaluate
from fintnet import health
from fintnet.telemetry import observability
from fintnet.telemetry.logging_config import log
from fintnet.models import JobRun, db
from fintnet.synthbank import store

bp = Blueprint("cron", __name__, url_prefix="/cron")


def _authorised() -> bool:
    secret = os.getenv("CRON_SECRET")
    if not secret:
        return not os.getenv("VERCEL")
    return request.headers.get("Authorization") == f"Bearer {secret}"


def _day(default_offset: int = 1) -> date:
    try:
        return date.fromisoformat(request.args.get("date", ""))
    except ValueError:
        return datetime.now(timezone.utc).date() - timedelta(days=default_offset)


def _run(job: str, day: date, fn):
    if not _authorised():
        return jsonify({"error": "unauthorised"}), 401
    force = request.args.get("force") == "1"
    run = JobRun.query.filter_by(job=job, run_date=day).first()
    now = datetime.now(timezone.utc)
    if run and not force:
        started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at and run.started_at.tzinfo is None \
            else run.started_at
        if run.status in ("ok", "warn"):
            return jsonify({"job": job, "date": day.isoformat(), "skipped": "already done", "details": run.details})
        if run.status == "running" and started and now - started < timedelta(minutes=15):
            return jsonify({"job": job, "date": day.isoformat(), "skipped": "in progress"})
    if run is None:
        run = JobRun(job=job, run_date=day)
        db.session.add(run)
    run.status, run.started_at, run.finished_at, run.details = "running", now, None, None
    db.session.commit()

    with observability.request(f"cron:{job}", input={"date": day.isoformat(), "force": force},
                               tags=["cron", job]) as obs:
        try:
            details = fn(day)
            status = details.get("status", "ok") if isinstance(details, dict) else "ok"
        except Exception as exc:  # noqa: BLE001 — record the failure, then report it
            db.session.rollback()
            details, status = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}, "error"
            log.exception("cron.failed", extra={"event": "cron.failed", "job": job})
        run = JobRun.query.filter_by(job=job, run_date=day).first()
        run.status, run.finished_at, run.details = status, datetime.now(timezone.utc), details
        db.session.commit()
        obs.update(output={"status": status, "details": details})
    observability.flush()
    code = 500 if status == "error" else 200
    return jsonify({"job": job, "date": day.isoformat(), "status": status, "details": details}), code


def _feed(day: date) -> dict:
    fed = store.feed_until(day)
    pruned = store.prune(datetime.now(timezone.utc).date())
    synced = store.sync_all_connections(current_app.config["SYNTHBANK_SYNC"])
    return {"status": "ok", "feed": fed, "prune": pruned, "sync": synced}


def _categorise(day: date) -> dict:
    result = cat.upgrade_provisional(limit=int(os.getenv("CATEGORISE_LIMIT", "150")))
    result["status"] = "ok"
    return result


def _evaluate(day: date) -> dict:
    return evaluate.run_daily(day, size=int(os.getenv("EVAL_SAMPLE", "150")))


def _health(day: date) -> dict:
    return health.run()


@bp.get("/feed")
def feed():
    return _run("feed", _day(1), _feed)


@bp.get("/categorise")
def categorise():
    return _run("categorise", _day(1), _categorise)


@bp.get("/evaluate")
def evaluate_route():
    return _run("evaluate", _day(1), _evaluate)


@bp.get("/health")
def health_route():
    return _run("health", _day(0), _health)


@bp.get("/status")
def status():
    if not _authorised():
        return jsonify({"error": "unauthorised"}), 401
    runs = JobRun.query.order_by(JobRun.run_date.desc(), JobRun.job).limit(40).all()
    return jsonify([{"job": r.job, "date": r.run_date.isoformat(), "status": r.status,
                     "started_at": r.started_at.isoformat() if r.started_at else None,
                     "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in runs])
