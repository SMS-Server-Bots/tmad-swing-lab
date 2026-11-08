from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def _write(relative: str, text: str) -> None:
    (ROOT / relative).write_text(text, encoding="utf-8", newline="")


def _insert_before(text: str, marker: str, addition: str, already: str) -> str:
    if already in text:
        return text
    count = text.count(marker)
    if count != 1:
        raise RuntimeError(f"marker {marker!r}: expected 1, got {count}")
    return text.replace(marker, addition + marker, 1)


def _replace_between(text: str, start: str, end: str, replacement: str, already: str, *, keep_end: bool = False) -> str:
    if already in text:
        return text
    if text.count(start) != 1:
        raise RuntimeError(f"start {start!r}: expected 1, got {text.count(start)}")
    begin = text.index(start)
    finish = text.find(end, begin + len(start))
    if finish < 0:
        raise RuntimeError(f"end marker missing after {start!r}: {end!r}")
    stop = finish if keep_end else finish + len(end)
    return text[:begin] + replacement + text[stop:]


def patch_tmad_client() -> None:
    path = "01_PROGRAMAS/data_bot/tmad_client.py"
    text = _read(path)
    marker = "    chosen_rows = complete_rows if complete_rows is not None else best_rows\n"
    addition = '''    # v6.1.43: TMAD Scanner is a moving ranking. If normal pagination
    # leaves a material hole, walk the same ranking in reverse and merge unique
    # symbols. Strict total/near-complete acceptance remains unchanged.
    if complete_rows is None and api_total and len(aggregate_rows) < api_total:
        normal_dir = str(SCANNER_PARAMS.get("dir") or "desc").strip().lower()
        reverse_dir = "asc" if normal_dir == "desc" else "desc"
        recovery_base = {**base, "dir": reverse_dir}
        for recovery_pass in range(2):
            for page in range(max_pages):
                params = {**recovery_base, "limit": size, "offset": page * size}
                page_payload, status = fetch_json_with_retries(client, SCANNER_URL, params=params)
                final_status = status
                page_rows = _rows_from_payload(page_payload)
                total = _total_count_from_payload(page_payload)
                if total is not None:
                    api_total = max(api_total, int(total))
                added = _merge_unique_rows(aggregate_rows, page_rows, aggregate_seen)
                debug.append({
                    "strategy": "reverse_offset_recovery", "pass": recovery_pass,
                    "page": page, "params": params, "status": status,
                    "rows": len(page_rows), "added_unique": added,
                    "unique_total": len(aggregate_rows), "api_total_count": total,
                })
                if len(aggregate_rows) == api_total:
                    complete_rows, complete_payload = list(aggregate_rows), page_payload
                    break
                if not page_rows or (added == 0 and page > 0):
                    break
            if complete_rows is not None:
                break

    if complete_rows is None and len(aggregate_rows) > len(best_rows):
        best_rows = list(aggregate_rows)

'''
    text = _insert_before(text, marker, addition, "reverse_offset_recovery")
    _write(path, text)


def patch_data_main() -> None:
    path = "01_PROGRAMAS/data_bot/main.py"
    text = _read(path)
    text = _insert_before(
        text,
        "from utils import rows_from_scanner_payload, ticker_from_row, to_float\n",
        "from gex_persistence import persist_gex_bundle\nfrom partial_risk_refresh import build_partial_risk_refresh\n",
        "from gex_persistence import persist_gex_bundle",
    )
    text = _insert_before(
        text,
        "    paired_gex_failures: list[dict] = []\n",
        "    gex_persist_seconds = 0.0\n",
        "    gex_persist_seconds = 0.0\n",
    )

    partial_marker = '''            write_output_json("latest_scanner_run.json", {
                "run_id": run_id, "session_type": session_type,
                "enrichment_profile": profile, "stored_ok": 0,
'''
    partial_addition = '''            try:
                risk_refresh = build_partial_risk_refresh(
                    scanner_run_id=run_id,
                    session_type=session_type,
                    rows=rows,
                    integrity=integrity,
                    client=client,
                )
                cprint(
                    "🛡️ [DATA RISK REFRESH] "
                    f"partial_run={run_id} open={len(risk_refresh.get('open_tickers') or [])} "
                    f"refreshed={len(risk_refresh.get('items') or [])} "
                    f"missing={len(risk_refresh.get('missing_open_tickers') or [])} "
                    "entries=DISABLED learning=DISABLED"
                )
            except Exception as risk_exc:
                cprint(f"⚠️ Position risk refresh no disponible para partial run={run_id}: {risk_exc}")
                log_quality(
                    'position_risk_refresh', 'warning', str(risk_exc), scanner_run_id=run_id,
                    metadata={'session_type': session_type, 'trace': traceback.format_exc()[:1800]},
                )
'''
    text = _insert_before(text, partial_marker, partial_addition, "🛡️ [DATA RISK REFRESH]")

    start = "                            insert_gex_summary({\n"
    end = "                            paired_gex_ok += 1\n"
    replacement = '''                            summary_row = {
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'current_price': current_price,
                                'change_percent': change_pct,
                                **gsum,
                                'profile_event_kind': profile_event.get('event_kind'),
                                'profile_hash': profile_event.get('profile_hash'),
                                'profile_changed_fields': profile_event.get('changed_fields', 0),
                                'profile_changed_strikes': profile_event.get('changed_strikes', 0),
                                'profile_changed_expiries': profile_event.get('changed_expiries', 0),
                                'profile_material_change': 1 if profile_event.get('material_change') else 0,
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                            }
                            level_rows = [{
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'current_price': current_price,
                                'change_percent': change_pct,
                                'distance_to_price_pct': distance_to_price_pct(lv.get('strike'), current_price),
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                                **lv,
                            } for lv in paired_levels]
                            expiration_rows = [{
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'current_price': current_price,
                                'change_percent': change_pct,
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                                **exp,
                            } for exp in paired_expirations]
                            bucket_rows = [{
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                                **bucket,
                            } for bucket in paired_buckets]
                            persist_started = time.perf_counter()
                            persisted = persist_gex_bundle(summary_row, level_rows, expiration_rows, bucket_rows)
                            gex_persist_seconds += time.perf_counter() - persist_started
                            paired_levels_count += int(persisted.get('levels') or 0)
                            paired_expirations_count += int(persisted.get('expirations') or 0)
                            paired_buckets_count += int(persisted.get('buckets') or 0)
                            paired_gex_ok += 1
'''
    text = _replace_between(
        text, start, end, replacement,
        "persisted = persist_gex_bundle(summary_row, level_rows, expiration_rows, bucket_rows)",
    )
    text = _insert_before(
        text,
        "            sample_text = ','.join(\n",
        '''            cprint(
                f"🗄️ [GEX PERSIST] scanner_run={run_id} gex_run={paired_gex_run_id} "
                f"sqlite_seconds={gex_persist_seconds:.2f} mode=atomic_per_ticker"
            )
''',
        "[GEX PERSIST] scanner_run=",
    )
    _write(path, text)


def patch_alerts_engine() -> None:
    path = "01_PROGRAMAS/alerts_bot/alerts_engine.py"
    text = _read(path)
    if "upsert_feature_observations_bulk" not in text.split("from db import (", 1)[1].split(")", 1)[0]:
        marker = "    insert_feature_observation,\n"
        if text.count(marker) != 1:
            raise RuntimeError("insert_feature_observation import marker missing")
        text = text.replace(marker, marker + "    upsert_feature_observations_bulk,\n", 1)
    text = _insert_before(
        text,
        "from lifecycle import update_trade_lifecycle\n",
        "from alerts_bulk_persistence import upsert_relational_contexts_bulk\n",
        "from alerts_bulk_persistence import upsert_relational_contexts_bulk",
    )
    marker = "    observations = 0\n    candidates: list[tuple[dict, dict, dict, dict]] = []\n"
    if "    observation_rows: list[dict] = []\n" not in text:
        if text.count(marker) != 1:
            raise RuntimeError("observation list marker missing")
        text = text.replace(
            marker,
            "    observations = 0\n    observation_rows: list[dict] = []\n    relational_rows: list[dict] = []\n    candidates: list[tuple[dict, dict, dict, dict]] = []\n",
            1,
        )

    start = "        if config.STORE_ALL_FEATURE_OBSERVATIONS:\n            if insert_feature_observation(feature_observation(snapshot, gex, features)):\n"
    end = "        if not entries_allowed:\n"
    replacement = '''        if config.STORE_ALL_FEATURE_OBSERVATIONS:
            observation_rows.append(feature_observation(snapshot, gex, features))
            relational = relational_by_ticker.get(ticker)
            if relational:
                relational_rows.append({
                    "source_scanner_run_id": int(run["id"]),
                    "snapshot_date": snapshot.get("snapshot_date"),
                    "ticker": ticker,
                    "sector_etf": relational.get("sector_etf"),
                    "market_regime": relational.get("market_regime"),
                    "market_context_score": relational.get("market_context_score"),
                    "market_breadth_score": relational.get("market_breadth_score"),
                    "vix_stress_score": relational.get("vix_stress_score"),
                    "credit_risk_on_score": relational.get("credit_risk_on_score"),
                    "sector_context_score": relational.get("sector_etf_score"),
                    "sector_breadth_score": relational.get("sector_breadth_score"),
                    "relationship_context_score": relational.get("relationship_context_score"),
                    "relationship_lead_lag_score": relational.get("relationship_lead_lag_score"),
                    "leveraged_confirmation_score": relational.get("leveraged_confirmation_score"),
                    "relative_strength_vs_market": relational.get("relative_strength_vs_market"),
                    "relative_strength_vs_sector": relational.get("relative_strength_vs_sector"),
                    "graph_context_score": relational.get("graph_context_score"),
                    "relational_deterioration_score": relational.get("relational_deterioration_score"),
                    "context_json": __import__("json").dumps(relational, ensure_ascii=False),
                    "unique_key": f"{int(run['id'])}|{ticker}",
                })
'''
    text = _replace_between(
        text, start, end, replacement,
        "observation_rows.append(feature_observation(snapshot, gex, features))",
        keep_end=True,
    )
    text = _insert_before(
        text,
        "    candidates.sort(\n",
        '''    persist_started = time.time()
    relational_persisted = 0
    if config.STORE_ALL_FEATURE_OBSERVATIONS:
        observations = upsert_feature_observations_bulk(observation_rows)
        relational_persisted = upsert_relational_contexts_bulk(relational_rows)
    persist_elapsed = time.time() - persist_started
    if observation_rows or relational_rows:
        log_info(
            "🗄️ [ALERTS PERSIST] "
            f"run={int(run.get('id') or 0)} features={observations}/{len(observation_rows)} "
            f"relational={relational_persisted}/{len(relational_rows)} sqlite_seconds={persist_elapsed:.2f} mode=bulk",
            send_discord=False,
        )

''',
        "🗄️ [ALERTS PERSIST]",
    )
    _write(path, text)


def patch_alerts_main() -> None:
    path = "01_PROGRAMAS/alerts_bot/main.py"
    text = _read(path)
    text = _insert_before(
        text,
        "from lifecycle import retry_pending_exit_deliveries\n",
        "from risk_refresh import maybe_process_partial_risk_refresh\n",
        "from risk_refresh import maybe_process_partial_risk_refresh",
    )
    text = _insert_before(
        text,
        "            if config.AUTO_SCAN_ON_NEW_DATA:\n",
        '''            # Position safety remains active even when global Scanner
            # integrity blocks the entry/research handoff. This path is lifecycle-only.
            try:
                maybe_process_partial_risk_refresh()
            except Exception as risk_exc:
                _log_info_throttled(
                    "partial_risk_refresh_error",
                    f"⚠️ Position risk refresh pospuesto: {risk_exc}",
                    interval_seconds=300,
                )

''',
        "partial_risk_refresh_error",
    )
    _write(path, text)


def main() -> int:
    patch_tmad_client()
    patch_data_main()
    patch_alerts_engine()
    patch_alerts_main()
    print("PASS | TMAD v6.1.43 patch applied/idempotent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
