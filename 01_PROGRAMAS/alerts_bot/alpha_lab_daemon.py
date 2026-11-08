from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from alpha_lab import build_alpha_lab_snapshot, write_alpha_lab_artifacts
from alpha_pnl_attribution import run_alpha_pnl_attribution
from scanner_rank_economics import run_scanner_rank_economics
from db import connect, get_runtime_state, set_runtime_state
from eod_options_runtime import run_alpha_swing_output, run_eod_options_shadow
from time_utils import ts_madrid_str

_MARKET_TZ = ZoneInfo("America/New_York")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return int(code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _latest_audit() -> dict:
    path = config.MARKET_OUTCOME_AUDITOR_OUTPUT_DIR / "latest_market_outcome_audit.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _latest_regime() -> dict:
    try:
        conn = connect(config.ALERTS_DB)
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "market_regime_history" not in tables:
                return {"regime": "UNKNOWN"}
            row = conn.execute("SELECT * FROM market_regime_history ORDER BY source_scanner_run_id DESC LIMIT 1").fetchone()
            if not row:
                return {"regime": "UNKNOWN"}
            item = dict(row)
            return {
                "regime": str(item.get("market_regime") or item.get("correlation_regime") or "UNKNOWN").upper(),
                "source_scanner_run_id": item.get("source_scanner_run_id"),
                "snapshot_date": item.get("snapshot_date"),
            }
        finally:
            conn.close()
    except Exception:
        return {"regime": "UNKNOWN"}


def _shadow_gate_policy_allows(snapshot: dict, *, min_mature_movements: int) -> bool:
    """Allow shared SHADOW gate mutation only after Auditor evidence is evaluable."""
    status = str(snapshot.get("audit_status") or "ACCUMULATING").upper()
    mature_movements = int(snapshot.get("mature_movements") or 0)
    if status == "ACCUMULATING":
        return False
    return mature_movements >= max(1, int(min_mature_movements))


def _persisted_eod_for_market_date(market_date: str) -> dict:
    try:
        raw = get_runtime_state("alpha_lab_eod_options_snapshot", "") or ""
        item = json.loads(raw) if raw else {}
        if str(item.get("asof") or "") != market_date:
            return {}
        if str(item.get("status") or "ok").lower() not in {"ok", "ready"}:
            return {}
        return item
    except Exception:
        return {}


def _eod_intraday_readiness() -> dict:
    market_date = datetime.now(_MARKET_TZ).date().isoformat()
    payload = {
        "market_date": market_date,
        "signal_rows": 0,
        "feature_rows": 0,
        "fusion_rows": 0,
        "latest_run": 0,
        "status": "WAITING_FOR_TODAY_SIGNALS",
    }
    try:
        conn = connect(config.ALERTS_DB)
        try:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "layer_signal_history" not in tables or "layer_feature_history" not in tables:
                payload["status"] = "WAITING_FOR_LAYER_SCHEMA"
                return payload
            payload["signal_rows"] = int(conn.execute(
                "SELECT COUNT(*) FROM layer_signal_history WHERE snapshot_date=?", (market_date,)
            ).fetchone()[0] or 0)
            payload["feature_rows"] = int(conn.execute(
                "SELECT COUNT(*) FROM layer_feature_history WHERE snapshot_date=?", (market_date,)
            ).fetchone()[0] or 0)
            payload["fusion_rows"] = int(conn.execute(
                "SELECT COUNT(*) FROM layer_signal_history WHERE snapshot_date=? AND UPPER(layer)='FUSION'", (market_date,)
            ).fetchone()[0] or 0)
            row = conn.execute(
                "SELECT MAX(source_scanner_run_id) FROM layer_signal_history WHERE snapshot_date=?", (market_date,)
            ).fetchone()
            payload["latest_run"] = int((row[0] if row else 0) or 0)
        finally:
            conn.close()
    except Exception as exc:
        payload["status"] = "READINESS_ERROR"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    if payload["feature_rows"] > 0 and payload["fusion_rows"] > 0:
        payload["status"] = "COLLECTING_INTRADAY"
    elif payload["signal_rows"] > 0 or payload["feature_rows"] > 0:
        payload["status"] = "PARTIAL_INTRADAY_MEMORY"
    else:
        finalized = _persisted_eod_for_market_date(market_date)
        if finalized:
            payload["status"] = "EOD_FINALIZED_ARCHIVED"
            payload["selected_count"] = int(finalized.get("selected_count") or 0)
            payload["source_scanner_run_id"] = int(finalized.get("source_scanner_run_id") or 0)
            payload["latest_run"] = payload["source_scanner_run_id"]
            payload["artifact_status"] = str(finalized.get("artifact_status") or "persisted")
    return payload


def run_once() -> dict:
    audit = _latest_audit()
    if not audit:
        return {"status": "waiting_for_audit"}
    regime = _latest_regime()
    snapshot = build_alpha_lab_snapshot(
        audit,
        regime=regime,
        current_research_gate=float(config.FUSION_MIN_ACTIONABLE_SCORE),
        research_gate_min=float(config.ADAPTIVE_RESEARCH_GATE_MIN),
        research_gate_max=float(config.ADAPTIVE_RESEARCH_GATE_MAX),
    )
    output_dir = config.OUTPUT_DIR / "alpha_lab"
    files = write_alpha_lab_artifacts(snapshot, output_dir, created_at_madrid=ts_madrid_str())
    snapshot["files"] = files

    readiness = _eod_intraday_readiness()
    set_runtime_state("alpha_lab_eod_readiness", json.dumps(readiness, ensure_ascii=False, default=str))
    set_runtime_state("alpha_lab_eod_readiness_status", str(readiness.get("status") or "UNKNOWN"))
    set_runtime_state("alpha_lab_eod_today_signal_rows", str(readiness.get("signal_rows") or 0))
    set_runtime_state("alpha_lab_eod_today_feature_rows", str(readiness.get("feature_rows") or 0))
    set_runtime_state("alpha_lab_eod_today_fusion_rows", str(readiness.get("fusion_rows") or 0))

    # Alpha Swing List is the user-facing product. It runs whenever a fresh
    # FUSION snapshot exists; OPEN/UPDATE/CLOSE stay SHADOW until their own
    # prospective economics justify promotion.
    swing = run_alpha_swing_output(max_positions=5)
    if swing.get("status") == "ok":
        set_runtime_state("alpha_swing_list_fingerprint", str(swing.get("fingerprint") or ""))
        set_runtime_state("alpha_swing_list_snapshot", json.dumps(swing, ensure_ascii=False, default=str))
        set_runtime_state("alpha_swing_list_source_run", str(swing.get("source_scanner_run_id") or 0))

    # EOD lane freezes the same product for economic attribution/options research.
    eod = run_eod_options_shadow(max_positions=5)
    if eod.get("status") == "ok":
        readiness["status"] = "EOD_READY"
        readiness["selected_count"] = int(eod.get("selected_count") or 0)
        readiness["source_scanner_run_id"] = int(eod.get("source_scanner_run_id") or 0)
        set_runtime_state("alpha_lab_eod_readiness", json.dumps(readiness, ensure_ascii=False, default=str))
        set_runtime_state("alpha_lab_eod_readiness_status", "EOD_READY")
        set_runtime_state("alpha_lab_eod_options_fingerprint", str(eod.get("fingerprint") or ""))
        set_runtime_state("alpha_lab_eod_options_snapshot", json.dumps(eod, ensure_ascii=False, default=str))
        set_runtime_state("alpha_lab_eod_options_source_run", str(eod.get("source_scanner_run_id") or 0))

    # Economic attribution is diagnostic only. It measures whether early discovery,
    # later confirmation and individual gates add or destroy economic value.
    # Never let this observer affect Alerts availability or mutate LIVE strategy.
    attribution = {"status": "not_run"}
    try:
        attribution_payload = run_alpha_pnl_attribution()
        attribution = {
            "status": "ok",
            "economic_opportunities": int((attribution_payload.get("audit_opportunity_cost") or {}).get("economic_opportunities") or 0),
            "confirmed_signal_sample": int((attribution_payload.get("confirmed_signal_pnl_proxy") or {}).get("sample") or 0),
        }
    except Exception as exc:
        attribution = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        set_runtime_state("alpha_pnl_attribution_status", "error")
        set_runtime_state("alpha_pnl_attribution_error", attribution["error"])

    # Scanner rank economics is a second, independent SHADOW observer.  It uses
    # only contemporaneous Scanner-native fields for ranking and only completed
    # future paths for labels.  It cannot mutate any trading parameter.
    rank_economics = {"status": "not_run"}
    try:
        rank_payload = run_scanner_rank_economics()
        rank_economics = {
            "status": str(rank_payload.get("status") or "ok"),
            "sample": int(rank_payload.get("mature_feature_observations") or 0),
            "scanner_runs": int(rank_payload.get("scanner_runs") or 0),
            "run_max": int(rank_payload.get("run_max") or 0),
        }
    except Exception as exc:
        rank_economics = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        set_runtime_state("scanner_rank_economics_status", "error")
        set_runtime_state("scanner_rank_economics_error", rank_economics["error"])

    entry_mode = str(get_runtime_state("entry_mode", "shadow") or "shadow").lower()
    baseline_gate = float(config.FUSION_MIN_ACTIONABLE_SCORE)
    experimental_gate = float((snapshot.get("shadow_parameters") or {}).get("research_gate_experimental") or baseline_gate)
    gate_policy_allows = _shadow_gate_policy_allows(
        snapshot,
        min_mature_movements=int(config.ADAPTIVE_MIN_MATURE_MOVEMENTS),
    )
    set_runtime_state("alpha_lab_mode", snapshot.get("mode") or "LIVE_DISCOVERY_SHADOW_EXECUTION")
    set_runtime_state("alpha_lab_fingerprint", snapshot.get("fingerprint") or "")
    set_runtime_state("alpha_lab_snapshot", json.dumps(snapshot, ensure_ascii=False, default=str))
    set_runtime_state("alpha_lab_research_gate_shadow", str(experimental_gate))
    set_runtime_state("alpha_lab_shadow_gate_policy_allows", "1" if gate_policy_allows else "0")
    if entry_mode != "live" and gate_policy_allows:
        set_runtime_state("adaptive_research_gate_shadow", str(experimental_gate))
        applied = experimental_gate
    else:
        if entry_mode != "live":
            set_runtime_state("adaptive_research_gate_shadow", str(baseline_gate))
        applied = baseline_gate
    return {
        "status": "ok",
        "audit_key": snapshot.get("audit_key"),
        "hypotheses": len(snapshot.get("hypotheses") or []),
        "active_experiments": snapshot.get("active_experiments") or [],
        "shadow_gate": experimental_gate,
        "applied_gate": applied,
        "shadow_gate_policy_allows": gate_policy_allows,
        "entry_mode": entry_mode,
        "fingerprint": snapshot.get("fingerprint"),
        "alpha_swing_status": swing.get("status"),
        "alpha_swing_selected": swing.get("selected_count", 0),
        "alpha_swing_open": int((swing.get("book_summary") or {}).get("open_count") or 0),
        "alpha_swing_events": len(swing.get("alpha_events") or []),
        "alpha_swing_source_run": swing.get("source_scanner_run_id", 0),
        "alpha_swing_artifact_status": swing.get("artifact_status"),
        "alpha_swing_closed_sample": int((((swing.get("book_summary") or {}).get("closed_economics") or {}).get("sample")) or 0),
        "alpha_swing_closed_avg": ((swing.get("book_summary") or {}).get("closed_economics") or {}).get("avg_return_pct"),
        "alpha_swing_closed_pf": ((swing.get("book_summary") or {}).get("closed_economics") or {}).get("profit_factor"),
        "eod_readiness_status": readiness.get("status"),
        "eod_today_signal_rows": readiness.get("signal_rows", 0),
        "eod_today_feature_rows": readiness.get("feature_rows", 0),
        "eod_today_fusion_rows": readiness.get("fusion_rows", 0),
        "eod_options_status": eod.get("status"),
        "eod_options_selected": eod.get("selected_count", 0),
        "eod_options_source_run": eod.get("source_scanner_run_id", 0),
        "eod_options_artifact_status": eod.get("artifact_status"),
        "pnl_attribution_status": attribution.get("status"),
        "pnl_attribution_opportunities": attribution.get("economic_opportunities", 0),
        "pnl_attribution_signal_sample": attribution.get("confirmed_signal_sample", 0),
        "scanner_rank_economics_status": rank_economics.get("status"),
        "scanner_rank_economics_sample": rank_economics.get("sample", 0),
        "scanner_rank_economics_runs": rank_economics.get("scanner_runs", 0),
        "scanner_rank_economics_run_max": rank_economics.get("run_max", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ready-file", default="")
    parser.add_argument("--ready-timeout", type=int, default=180)
    args = parser.parse_args()

    if args.ready_file:
        ready_path = Path(args.ready_file)
        deadline = time.monotonic() + max(10, int(args.ready_timeout))
        while not ready_path.exists():
            if args.parent_pid and not _pid_alive(args.parent_pid):
                return 0
            if time.monotonic() >= deadline:
                print(f"[ALPHA LAB WARN] ready-file timeout: {ready_path}", flush=True)
                return 0
            time.sleep(1)

    last_fingerprint = ""
    last_swing_key = ""
    last_eod_key = ""
    last_readiness = ""
    last_attribution = ""
    last_rank_economics = ""
    while True:
        if args.parent_pid and not _pid_alive(args.parent_pid):
            return 0
        try:
            result = run_once()
            fingerprint = str(result.get("fingerprint") or "")
            if fingerprint and fingerprint != last_fingerprint:
                print(
                    "[ALPHA LAB] status={status} audit={audit} hypotheses={hyp} gate={gate} mode={mode} experiments={exp}".format(
                        status=result.get("status"), audit=result.get("audit_key"), hyp=result.get("hypotheses", 0),
                        gate=result.get("applied_gate"), mode=result.get("entry_mode"),
                        exp=",".join(result.get("active_experiments") or []) or "none",
                    ),
                    flush=True,
                )
                last_fingerprint = fingerprint
            readiness_key = f"{result.get('eod_readiness_status')}:{result.get('eod_today_signal_rows')}:{result.get('eod_today_feature_rows')}:{result.get('eod_today_fusion_rows')}"
            if readiness_key != last_readiness:
                print(
                    f"[EOD READINESS] status={result.get('eod_readiness_status')} signals={result.get('eod_today_signal_rows')} features={result.get('eod_today_feature_rows')} fusion={result.get('eod_today_fusion_rows')}",
                    flush=True,
                )
                last_readiness = readiness_key
            swing_key = f"{result.get('alpha_swing_source_run')}:{result.get('alpha_swing_artifact_status')}:{result.get('alpha_swing_selected')}:{result.get('alpha_swing_open')}:{result.get('alpha_swing_events')}:{result.get('alpha_swing_closed_sample')}"
            if result.get("alpha_swing_status") == "ok" and swing_key != last_swing_key:
                print(
                    f"[ALPHA SWING LIST] run={result.get('alpha_swing_source_run')} selected={result.get('alpha_swing_selected')} open={result.get('alpha_swing_open')} events={result.get('alpha_swing_events')} closed={result.get('alpha_swing_closed_sample')} avg={result.get('alpha_swing_closed_avg')} pf={result.get('alpha_swing_closed_pf')} artifact={result.get('alpha_swing_artifact_status')}",
                    flush=True,
                )
                last_swing_key = swing_key
            eod_key = f"{result.get('eod_options_source_run')}:{result.get('eod_options_artifact_status')}:{result.get('eod_options_selected')}"
            if result.get("eod_options_status") == "ok" and eod_key != last_eod_key:
                print(
                    f"[EOD OPTIONS SHADOW] run={result.get('eod_options_source_run')} selected={result.get('eod_options_selected')} artifact={result.get('eod_options_artifact_status')}",
                    flush=True,
                )
                last_eod_key = eod_key
            attribution_key = f"{result.get('pnl_attribution_status')}:{result.get('pnl_attribution_opportunities')}:{result.get('pnl_attribution_signal_sample')}"
            if attribution_key != last_attribution:
                print(
                    f"[ALPHA PNL ATTRIBUTION] status={result.get('pnl_attribution_status')} opportunities={result.get('pnl_attribution_opportunities')} dedup_signal_sample={result.get('pnl_attribution_signal_sample')}",
                    flush=True,
                )
                last_attribution = attribution_key
            rank_key = f"{result.get('scanner_rank_economics_status')}:{result.get('scanner_rank_economics_sample')}:{result.get('scanner_rank_economics_runs')}:{result.get('scanner_rank_economics_run_max')}"
            if rank_key != last_rank_economics:
                print(
                    f"[SCANNER RANK ECONOMICS] status={result.get('scanner_rank_economics_status')} mature={result.get('scanner_rank_economics_sample')} runs={result.get('scanner_rank_economics_runs')} latest_run={result.get('scanner_rank_economics_run_max')}",
                    flush=True,
                )
                last_rank_economics = rank_key
            if args.once:
                return 0 if result.get("status") in {"ok", "waiting_for_audit"} else 1
        except sqlite3.OperationalError as exc:
            print(f"[ALPHA LAB WARN] sqlite busy/deferred: {exc}", flush=True)
            if args.once:
                return 0
        except Exception as exc:
            print(f"[ALPHA LAB WARN] non-fatal: {type(exc).__name__}: {exc}", flush=True)
            if args.once:
                return 0
        time.sleep(max(15, int(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())