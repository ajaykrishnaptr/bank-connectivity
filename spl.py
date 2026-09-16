"""
A small search language over the event log, shaped like Splunk's SPL.

    event=assistant.* | stats count by event
    sourcetype=fintnet:tool latency_ms>500 | table time, event, latency_ms | head 20
    cron status=error

Supported:
  * Base search: `field=value`, `field!=value`, `field>value`, `field<value`,
    `field>=`, `field<=`, values with `*` wildcards, quoted values, and bare
    words matched anywhere in the event. Terms are ANDed.
  * `| stats count [as name] [by field[, field]]`, also avg(x), sum(x),
    min(x), max(x).
  * `| table field, field`, `| fields field, field` (the same thing).
  * `| sort field` or `| sort -field`.
  * `| head n`.

Fields come from the HEC record: `time`, `host`, `source`, `sourcetype`, and
every key inside `event`, flattened with dots (`context.bank`). Unknown
commands raise SplError, which the page shows as a message.
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from datetime import datetime, timezone
from typing import Any

_OPS = ("!=", ">=", "<=", "=", ">", "<")
_STAT = re.compile(r"^(count|avg|sum|min|max)\(([^)]*)\)$|^(count)$", re.I)


class SplError(ValueError):
    """The query could not be parsed or used."""


def flatten(record: dict, prefix: str = "") -> dict[str, Any]:
    """Event record as flat dotted fields, the way SPL addresses them."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{name}."))
        else:
            out[name] = value
    return out


def row_for(record: dict) -> dict[str, Any]:
    """One searchable row: the record's own fields plus the event's, undotted where possible."""
    row = {k: v for k, v in record.items() if k != "event"}
    row.update(flatten(record.get("event") or {}))
    row["_raw"] = record
    row["time"] = _iso(record.get("time"))
    return row


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts or "")


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _matches(row: dict, field: str, op: str, value: str) -> bool:
    if field not in row:
        return op == "!="
    actual = row[field]
    if op in (">", "<", ">=", "<="):
        left, right = _as_number(actual), _as_number(value)
        if left is None or right is None:
            return False
        return {">": left > right, "<": left < right, ">=": left >= right, "<=": left <= right}[op]
    text = str(actual)
    hit = fnmatch.fnmatchcase(text, value) if "*" in value else text.lower() == value.lower()
    return not hit if op == "!=" else hit


def _term(row: dict, term: str) -> bool:
    for op in _OPS:
        if op in term:
            field, value = term.split(op, 1)
            if field and not field.endswith(("!", ">", "<", "=")):
                return _matches(row, field.strip(), op, value.strip())
    needle = term.lower()
    return any(needle in str(v).lower() for k, v in row.items() if k != "_raw")


def _search(rows: list[dict], query: str) -> list[dict]:
    if not query.strip():
        return rows
    try:
        terms = shlex.split(query)
    except ValueError as exc:
        raise SplError(f"could not read the search: {exc}") from exc
    return [r for r in rows if all(_term(r, t) for t in terms)]


def _stats(rows: list[dict], args: str) -> tuple[list[dict], list[str]]:
    by: list[str] = []
    if " by " in f" {args} ":
        args, _, by_part = args.partition(" by ")
        by = [f.strip() for f in by_part.replace(",", " ").split() if f.strip()]
    aggs: list[tuple[str, str, str]] = []           # (label, function, field)
    for piece in [p.strip() for p in args.split(",") if p.strip()]:
        piece, _, alias = piece.partition(" as ")
        m = _STAT.match(piece.strip())
        if not m:
            raise SplError(f"stats does not know '{piece.strip()}'")
        fn = (m.group(1) or m.group(3)).lower()
        field = (m.group(2) or "").strip()
        aggs.append((alias.strip() or (f"{fn}({field})" if field else "count"), fn, field))
    if not aggs:
        aggs = [("count", "count", "")]

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(str(r.get(f, "")) for f in by), []).append(r)
    out = []
    for key, members in groups.items():
        row = dict(zip(by, key))
        for label, fn, field in aggs:
            if fn == "count":
                row[label] = len(members)
                continue
            numbers = [n for n in (_as_number(m.get(field)) for m in members) if n is not None]
            if not numbers:
                row[label] = None
            else:
                row[label] = round({"avg": sum(numbers) / len(numbers), "sum": sum(numbers),
                                    "min": min(numbers), "max": max(numbers)}[fn], 2)
        out.append(row)
    first = aggs[0][0]
    out.sort(key=lambda r: (r.get(first) is None, r.get(first)), reverse=True)
    return out, by + [label for label, _, _ in aggs]


def run(query: str, records: list[dict]) -> dict:
    """Run `query` over HEC records. Returns {"rows", "columns", "stats"}."""
    parts = [p.strip() for p in (query or "").split("|")]
    rows = [row_for(r) for r in records]
    rows = _search(rows, parts[0])
    columns: list[str] = []
    is_stats = False

    for part in parts[1:]:
        if not part:
            continue
        name, _, args = part.partition(" ")
        name, args = name.lower(), args.strip()
        if name == "stats":
            rows, columns = _stats(rows, args)
            is_stats = True
        elif name in ("table", "fields"):
            columns = [f.strip() for f in args.replace(",", " ").split() if f.strip()]
            rows = [{c: r.get(c) for c in columns} | ({} if is_stats else {"_raw": r.get("_raw")}) for r in rows]
        elif name == "sort":
            for field in reversed([f.strip() for f in args.replace(",", " ").split() if f.strip()]):
                desc = field.startswith("-")
                field = field.lstrip("-+")
                rows.sort(key=lambda r, f=field: (r.get(f) is None, str(r.get(f, ""))), reverse=desc)
        elif name == "head":
            try:
                rows = rows[:max(1, int(args or 10))]
            except ValueError as exc:
                raise SplError("head needs a number") from exc
        else:
            raise SplError(f"unknown command '{name}'")
    return {"rows": rows, "columns": columns, "stats": is_stats}
