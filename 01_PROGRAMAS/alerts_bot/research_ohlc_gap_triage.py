from __future__ import annotations

"""Fail-closed historical gap triage + optional alternate-source audit.

This worker never writes LIVE, canonical historical prices, or the research
OHLC overlay. Stooq is diagnostic only; unadjusted/adjusted basis must be
independently reconciled before any future promotion.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import argparse
import csv
import io
import json
import math
import os
import sqlite3
import time

import config
from research_ohlc_backfill import (
    NY, RESEARCH_DB, _required_keys, _request_windows,
)
from research_swing_exit_policy import _future_path, _load_db_price_paths, _valid_ohlc

REPORT = config.OUTPUT_DIR / "historical_validation" / "latest_research_ohlc_gap_triage.json"
STRESS_REPORT = config.OUTPUT_DIR / "historical_validation" / "latest_joint_entry_exit_stress.json"
BACKFILL_REPORT = config.OUTPUT_DIR / "historical_validation" / "latest_research_ohlc_backfill.json"
MODE = "TMAD_RESEARCH_OHLC_GAP_TRIAGE_V1"
ALT_SOURCE = "STOOQ_CSV_SECONDARY_DIAGNOSTIC_ONLY"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _classify(reason: str, cause: str) -> str:
    value = (reason + " " + cause).upper()
    if "SPLIT" in value or "DISCONTINUITY" in value:
        return "CORPORATE_ACTION_OR_PRICE_DISCONTINUITY"
    if "CONFLICT" in value or "BASIS" in value:
        return "PRICE_BASIS_CONFLICT"
    if "404" in value or "SYMBOL" in value:
        return "SYMBOL_MISSING_OR_DELISTED"
    if "MISSING_OHLC" in value or "INVALID_OR_CLOSE_ONLY" in value or "INCOMPLETE_OHLC" in value:
        return "INCOMPLETE_OR_INVALID_BARS"
    if "STALE" in value:
        return "STALE_OR_MISSING_FORWARD_WINDOW"
    return "INSUFFICIENT_FORWARD_HISTORY"


def _alt_symbol(ticker: str) -> str | None:
    # No speculative share-class, delisted, OTC or foreign-exchange mapping.
    symbol = ticker.strip().upper()
    if not 1 <= len(symbol) <= 6 or not symbol.isalnum() or not symbol.isascii():
        return None
    return symbol.lower() + ".us"


def _fetch_stooq(ticker: str, start: date, end: date, api_key: str):
    """Independent untrusted *candidate* OHLC. Never write to an OHLC DB."""
    symbol = _alt_symbol(ticker)
    if not symbol:
        return [], "ALT_SYMBOL_NOT_SUPPORTED"
    if not api_key:
        return [], "ALT_KEY_NOT_CONFIGURED"
    params = {
        "s": symbol, "d1": start.strftime("%Y%m%d"),
        "d2": end.strftime("%Y%m%d"), "i": "d", "apikey": api_key,
    }
    req = Request(
        "https://stooq.com/q/d/l/?" + urlencode(params),
        headers={"User-Agent": "TMADResearchGapAudit/1.0",
                 "Accept": "text/csv"},
    )
    try:
        with urlopen(req, timeout=20) as response:
            if int(response.status) != 200:
                return [], "ALT_HTTP_" + str(response.status)
            body = response.read(700_000).decode("utf-8-sig", errors="replace")
    except HTTPError as exc:
        return [], "ALT_HTTP_" + str(exc.code)
    except (URLError, OSError, TimeoutError):
        return [], "ALT_NETWORK_ERROR"
    if body.lstrip()[:4].lower() != "date":
        return [], "ALT_UNVERIFIED_CSV_OR_QUOTA"
    reader = csv.DictReader(io.StringIO(body))
    if not reader.fieldnames or not {"Date", "Open", "High", "Low", "Close"}.issubset(reader.fieldnames):
        return [], "ALT_BAD_COLUMNS"
    output = []
    seen = set()
    try:
        for row in reader:
            day = date.fromisoformat(row["Date"])
            if not start <= day <= end:
                continue
            if day in seen:
                return [], "ALT_DUPLICATE_DATE"
            seen.add(day)
            vals = [float(row[k]) for k in ("Open", "High", "Low", "Close")]
            if any(not math.isfinite(v) or v <= 0 for v in vals):
                return [], "ALT_INVALID_OHLC"
            op, hi, lo, close = vals
            if lo > min(op, close) or hi < max(op, close) or lo > hi:
                return [], "ALT_BAD_GEOMETRY"
            output.append({
                "trade_date": day.isoformat(), "open": op,
                "high": hi, "low": lo, "close": close,
                "split_factor": 1.0, "source": ALT_SOURCE,
            })
    except (ValueError, TypeError, KeyError):
        return [], "ALT_BAD_ROW"
    return sorted(output, key=lambda x: x["trade_date"]), None if output else "ALT_EMPTY_WINDOW"


def _audit_alt(ticker: str, dates: list[str], reference_paths: dict, api_key: str, fetcher, cutoff: date):
    windows = _request_windows(dates, cutoff)
    obtained = {}
    failures = []
    for start, end in windows:
        bars, error = fetcher(ticker, start, end, api_key)
        if error:
            failures.append(error)
            continue
        for bar in bars:
            prev = obtained.get(bar["trade_date"])
            if prev and abs(float(prev["close"]) / float(bar["close"]) - 1) > 0.01:
                failures.append("ALT_INTERNAL_WINDOW_CONFLICT")
                break
            obtained[bar["trade_date"]] = bar
    rows = sorted(obtained.values(), key=lambda x: x["trade_date"])
    tested = Counter()
    for day in dates:
        _, error = _future_path({"signal_date": day}, rows, sessions=10)
        tested[str(error or "COMPLETE")] += 1
    # Same-date original full OHLC observations, NOT close-only synthetic bars.
    anchors = {}
    for day in dates:
        for bar in reference_paths.get((ticker, day), []):
            key = str(bar.get("trade_date") or "")[:10]
            if _valid_ohlc(bar, signal_date=day) is not None:
                anchors[key] = bar
    ratios = [
        abs(float(bar["close"]) / float(anchors[bar["trade_date"]]["close"]) - 1)
        for bar in rows if bar["trade_date"] in anchors
        and float(anchors[bar["trade_date"]].get("close") or 0) > 0
    ]
    # No reliance on Stooq as authoritative, nor reinterpretation of split
    # adjustments; matching closes is a *possible* review target only.
    if any(x.startswith("ALT_") for x in failures):
        status = "QUARANTINE_ALTERNATE_SOURCE"
    elif tested.get("COMPLETE", 0) == 0:
        status = "ALTERNATE_INCOMPLETE"
    elif len(ratios) < 2:
        status = "UNVERIFIED_PRICE_BASIS"
    elif max(ratios) > 0.02:
        status = "ALTERNATE_PRICE_BASIS_CONFLICT"
    else:
        status = "MANUAL_REVIEW_CANDIDATE"
    return {
        "status": status, "signal_keys": len(dates),
        "complete_alternate_paths": tested.get("COMPLETE", 0),
        "cross_source_overlap_sessions": len(ratios),
        "worst_close_difference_pct": round(max(ratios) * 100, 3) if ratios else None,
        "windows_requested": len(windows), "failed_windows": failures,
        "no_automatic_overlay_insertion": True,
        "correction_for_splits_not_verified": True,
    }


def _readiness(stress: dict, price_revision: str) -> dict:
    shortlist = stress.get("train_frozen_shortlist") or []
    validation = stress.get("validation") or {}
    revision = str((stress.get("methodology") or {}).get("research_ohlc_revision") or "")
    stale = not stress or revision != price_revision
    candidates = []
    for item in shortlist:
        key = str(item.get("id") or "")
        arms = {}
        blockers = []
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            row = ((validation.get(lane) or {}).get("specs") or {}).get(key) or {}
            n, days = int(row.get("evaluable") or 0), int(row.get("dates") or 0)
            avg, pf = row.get("net_avg_pct"), row.get("net_profit_factor")
            arms[lane] = {
                "trades": n, "distinct_dates": days,
                "avg_net_pct": avg, "profit_factor": pf,
                "explicit_sector_etf_trades": int(row.get("explicit_sector_etf_trades") or 0),
                "slot_admission_pct": row.get("portfolio_slot_admission_pct"),
            }
            if n < 20:
                blockers.append(lane + "_TRADES_BELOW_20")
            if days < 8:
                blockers.append(lane + "_DATES_BELOW_8")
            if avg is None or float(avg) <= 0 or pf is None or float(pf) <= 1:
                blockers.append(lane + "_NONPOSITIVE_EDGE")
        blockers.extend([
            "OVERLAPPING_VALIDATION_LANES_NOT_INDEPENDENT",
            "PORTFOLIO_CAPITAL_AND_OPTIONS_NOT_EVALUATED",
            "PROSPECTIVE_SHADOW_NOT_EVALUATED",
        ])
        if stale:
            blockers.append("PRICE_REVISION_MISMATCH")
        candidates.append({
            "train_frozen_id": key, "rolling_fixed": arms,
            "issues": blockers, "eligible_to_promote": False,
        })
    return {
        "joint_report_generated_at_madrid": stress.get("generated_at_madrid"),
        "joint_report_research_ohlc_revision": revision,
        "current_research_ohlc_revision": price_revision,
        "joint_report_revision_matches": not stale,
        "train_frozen_shortlist_size": len(shortlist),
        "candidates": candidates, "validation_used_for_selection": False,
        "live_promotion_allowed": False, "final_test": "LOCKED",
    }


def run(*, max_secondary_tickers: int = 40, pause_seconds: float = 1.5,
        alternate_fetcher=None, api_key: str | None = None) -> dict:
    needed, evidence = _required_keys()
    rows = [{"ticker": ticker, "signal_date": day}
            for ticker, days in needed.items() for day in days]
    price_paths, price_diag = _load_db_price_paths(rows)
    cutoff = datetime.now(NY).date() - timedelta(days=1)
    open_gaps = defaultdict(list)
    cause_count = Counter()
    by_ticker = {}
    req = {}
    if Path(RESEARCH_DB).is_file():
        conn = sqlite3.connect(f"file:{Path(RESEARCH_DB).resolve().as_posix()}?mode=ro", uri=True)
        try:
            req = {ticker: {"status": status, "reason": reason or ""}
                   for ticker, status, reason in conn.execute(
                       "SELECT ticker,status,reason FROM requests"
                   )}
        finally:
            conn.close()
    for ticker, dates in sorted(needed.items()):
        affected = []
        for day in dates:
            # Same mature definition as the backfill; keep not-mature separate.
            if date.fromisoformat(day) > cutoff - timedelta(days=22):
                continue
            path, reason = _future_path(
                {"signal_date": day}, price_paths.get((ticker, day), []), sessions=10
            )
            if reason:
                affected.append({
                    "signal_date": day, "forward_error": str(reason),
                    "usable_forward_bars": len(path),
                })
        if affected:
            last = req.get(ticker) or {}
            provider_error = str(last.get("reason") or "")
            category = _classify(provider_error, affected[0]["forward_error"])
            cause_count[category] += len(affected)
            open_gaps[ticker] = [a["signal_date"] for a in affected]
            by_ticker[ticker] = {
                "cause": category,
                "request_status": last.get("status") or "NO_REQUEST",
                "provider_errors": provider_error,
                "signal_keys": len(affected),
                "missing_signals": affected,
                "manual_reconciliation_required": True,
            }
    configured = api_key if api_key is not None else os.environ.get("TMAD_RESEARCH_STOOQ_API_KEY", "")
    secondary = {
        "provider": ALT_SOURCE, "provider_key_configured": bool(configured),
        "diagnostic_only": True, "status": "NO_KEY_DIAGNOSTIC_ONLY",
        "attempted_tickers": 0, "cases": {},
        "no_live_or_research_overlay_mutation": True,
    }
    if configured and max_secondary_tickers > 0:
        secondary["status"] = "AUDITED"
        # Focus on smallest/least ambiguous groups; no automatic acceptance.
        fetcher = alternate_fetcher or _fetch_stooq
        for ticker, dates in list(sorted(open_gaps.items(), key=lambda kv: (len(kv[1]), kv[0])))[:max_secondary_tickers]:
            audit = _audit_alt(ticker, dates, price_paths, configured, fetcher, cutoff)
            secondary["cases"][ticker] = audit
            secondary["attempted_tickers"] += 1
            if "ALT_UNVERIFIED_CSV_OR_QUOTA" in audit["failed_windows"] or "ALT_HTTP_429" in audit["failed_windows"]:
                secondary["status"] = "STOPPED_PROVIDER_LIMIT"
                break
            if pause_seconds:
                time.sleep(pause_seconds)
    price_manifest = _read(BACKFILL_REPORT)
    price_rev = str((price_manifest.get("outcomes") or {}).get("revision") or "NONE")
    return {
        "mode": MODE,
        "generated_at_madrid": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "source_cache": evidence.get("cache"),
        "final_test_locked": True, "live_mutation": False,
        "canonical_price_mutation": False, "research_ohlc_mutation": False,
        "required_signal_keys": evidence.get("signal_keys"),
        "residual": {
            "missing_10session_signal_keys": sum(x["signal_keys"] for x in by_ticker.values()),
            "affected_tickers": len(by_ticker),
            "causes_by_signal": dict(cause_count),
            "by_ticker": by_ticker,
        },
        "secondary_source": secondary,
        "price_loader_diagnostics": price_diag,
        "economic_validation": _readiness(_read(STRESS_REPORT), price_rev),
        "next_step": "Resolve blocked splits/price bases with independently licensed provider; validate more independent dates and prospective SHADOW before LIVE",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-secondary-tickers", type=int, default=40)
    parser.add_argument("--pause-seconds", type=float, default=1.5)
    args = parser.parse_args()
    if not 0 <= args.max_secondary_tickers <= 100 or not 0 <= args.pause_seconds <= 60:
        parser.error("max-secondary-tickers 0..100; pause-seconds 0..60")
    report = run(max_secondary_tickers=args.max_secondary_tickers, pause_seconds=args.pause_seconds)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, REPORT)
    print(json.dumps({
        "artifact": str(REPORT), "gaps": report["residual"]["missing_10session_signal_keys"],
        "tickers": report["residual"]["affected_tickers"],
        "secondary": report["secondary_source"]["status"],
        "shortlisted": report["economic_validation"]["train_frozen_shortlist_size"],
        "promotion_allowed": False, "no_full": True, "live_unchanged": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
