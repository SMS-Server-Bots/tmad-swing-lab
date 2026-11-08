from __future__ import annotations

"""Read-only Scanner/SELECTIVE alpha report from the published runtime-data projection.

The report deliberately separates discovery quality from downstream gate quality. It
never changes trading parameters. Pending/incomplete outcomes must not be supplied as
mature events by callers.
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _load_projection(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return [dict(row) for row in (rows or []) if isinstance(row, dict)]


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_run_id(event: dict[str, Any]) -> int:
    details = _json_dict(event.get("details_json"))
    candidates = [event.get("source_scanner_run_id"), details.get("source_scanner_run_id")]
    row = details.get("row")
    if isinstance(row, dict):
        candidates.append(row.get("source_scanner_run_id"))
    detail_rows = details.get("details")
    if isinstance(detail_rows, list):
        for item in detail_rows:
            if isinstance(item, dict):
                source = item.get("source_scanner_run_id")
                if source is None and isinstance(item.get("row"), dict):
                    source = item["row"].get("source_scanner_run_id")
                candidates.append(source)
    for value in candidates:
        try:
            run_id = int(value or 0)
        except (TypeError, ValueError):
            continue
        if run_id > 0:
            return run_id
    return 0


def _pct(num: int, den: int) -> float | None:
    return round(num / den * 100.0, 3) if den else None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "none", "null", "no"}
    return bool(value)


def _selection_index(snapshot_rows: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
    """Infer SELECTIVE membership from persisted scanner snapshots.

    Base scanner-only rows use pending for selected tickers and skipped/not_selected for
    non-selected tickers. Enriched rows later become ok/error. This lets QA reconstruct
    membership even when the per-run CSV/JSON selection report has already rolled over.
    """
    index: dict[tuple[int, str], dict[str, Any]] = {}
    for row in snapshot_rows:
        try:
            run_id = int(row.get("scanner_run_id") or 0)
        except (TypeError, ValueError):
            continue
        ticker = str(row.get("ticker") or "").upper().strip()
        if not run_id or not ticker:
            continue
        statuses = [str(row.get(k) or "").lower() for k in ("detail_status", "intraday_status", "gex_status")]
        error = str(row.get("error_message") or "").lower()
        selected = any(s in {"pending", "ok", "error", "unavailable_cached"} for s in statuses) and "not_selected:" not in error
        enriched = any(s in {"ok", "error", "unavailable_cached"} for s in statuses)
        key = (run_id, ticker)
        previous = index.get(key) or {"selected": False, "enriched": False}
        index[key] = {"selected": bool(previous["selected"] or selected), "enriched": bool(previous["enriched"] or enriched)}
    return index


def _miss_class(event: dict[str, Any], membership: dict[str, Any] | None) -> str:
    existing = str(event.get("classification") or "").upper().strip()
    if membership is not None and not membership.get("selected"):
        return "NOT_SELECTED"
    if existing in {"WRONG_DIRECTION", "DETECTED_LATE"}:
        return existing
    if not _truthy(event.get("core_detected")):
        return "NO_CORE"
    if not _truthy(event.get("research_reached")):
        return "RESEARCH_BLOCK"
    if not _truthy(event.get("gex_confirmed")):
        details = _json_dict(event.get("details_json"))
        text = json.dumps(details, ensure_ascii=False).upper()
        return "GEX_DIVERGENCE" if "DIVERGENCE" in text else "GEX_BLOCK"
    if not _truthy(event.get("timing_ready")):
        return "TIMING_NOT_READY"
    if not _truthy(event.get("fusion_ready")):
        return "FUSION_BLOCK"
    if not _truthy(event.get("enter_generated")):
        return "FUSION_BLOCK"
    return existing or "OTHER"


def aggregate_by_scanner_run(events: list[dict[str, Any]], snapshot_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    membership_index = _selection_index(snapshot_rows or [])
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    unknown = 0
    for event in events:
        run_id = _source_run_id(event)
        if run_id <= 0:
            unknown += 1
            continue
        grouped[run_id].append(event)

    runs: list[dict[str, Any]] = []
    global_classes: Counter[str] = Counter()
    known_membership = selected_pre_hit = enriched_pre_hit = 0
    for run_id in sorted(grouped, reverse=True):
        rows = grouped[run_id]
        total = len(rows)
        stages = {
            "core_detected": sum(_truthy(row.get("core_detected")) for row in rows),
            "research_reached": sum(_truthy(row.get("research_reached")) for row in rows),
            "gex_confirmed": sum(_truthy(row.get("gex_confirmed")) for row in rows),
            "timing_ready": sum(_truthy(row.get("timing_ready")) for row in rows),
            "fusion_ready": sum(_truthy(row.get("fusion_ready")) for row in rows),
            "enter_generated": sum(_truthy(row.get("enter_generated")) for row in rows),
        }
        classes: Counter[str] = Counter()
        run_known = run_selected = run_enriched = 0
        leads: list[float] = []
        for row in rows:
            ticker = str(row.get("ticker") or "").upper().strip()
            membership = membership_index.get((run_id, ticker)) if ticker else None
            classes[_miss_class(row, membership)] += 1
            if membership is not None:
                run_known += 1
                run_selected += int(bool(membership.get("selected")))
                run_enriched += int(bool(membership.get("enriched")))
            try:
                leads.append(float(row.get("lead_sessions")))
            except (TypeError, ValueError):
                pass
        global_classes.update(classes)
        known_membership += run_known
        selected_pre_hit += run_selected
        enriched_pre_hit += run_enriched
        runs.append({
            "scanner_run_id": run_id,
            "mature_movement_events": total,
            "selection_evidence": {
                "events_with_membership_evidence": run_known,
                "selected_pre_hit": run_selected,
                "enriched_pre_hit": run_enriched,
                "selective_pre_hit_recall_pct": _pct(run_selected, run_known),
                "enriched_pre_hit_recall_pct": _pct(run_enriched, run_known),
            },
            "funnel": {key: {"count": value, "recall_pct": _pct(value, total)} for key, value in stages.items()},
            "miss_classifications": dict(classes),
            "avg_lead_sessions": round(sum(leads) / len(leads), 3) if leads else None,
        })

    return {
        "schema_version": 2,
        "scope": "mature_market_outcome_audit_events_by_scanner_run",
        "event_count": sum(len(rows) for rows in grouped.values()),
        "events_without_scanner_run_id": unknown,
        "selection_evidence": {
            "events_with_membership_evidence": known_membership,
            "selected_pre_hit": selected_pre_hit,
            "enriched_pre_hit": enriched_pre_hit,
            "selective_pre_hit_recall_pct": _pct(selected_pre_hit, known_membership),
            "enriched_pre_hit_recall_pct": _pct(enriched_pre_hit, known_membership),
            "coverage_warning": "Recall is evaluable only over events whose source run/ticker snapshot is present in the exported projection." if known_membership < sum(len(v) for v in grouped.values()) else None,
        },
        "miss_classifications": dict(global_classes),
        "runs": runs,
        "notes": [
            "Use mature Market Outcome Auditor events only; never count PENDING/incomplete outcomes as misses.",
            "SELECTIVE membership is reconstructed from scanner_snapshots statuses, independent of the rolling latest_enrichment_selection file.",
            "Do not change strategic thresholds from this report until Auditor sample requirements are met; low-risk observability fixes are exempt.",
            "Optimize expectancy/utility and stable out-of-sample alpha, not hit-rate alone.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-data", type=Path, default=Path("runtime_data"))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    alerts = args.runtime_data / "tables" / "alerts_bot__db__live__tmad_alerts_adaptive.db" / "market_outcome_audit_events.json"
    snapshots = args.runtime_data / "tables" / "data_bot__db__live__tmad_scanner_live.db" / "scanner_snapshots.json"
    report = aggregate_by_scanner_run(_load_projection(alerts), _load_projection(snapshots))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
