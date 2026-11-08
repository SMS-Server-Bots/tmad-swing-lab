from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime

import config
from db import daily_price_series, observations_needing_labels_batch, upsert_observation_outcomes_bulk
from discord_utils import log_info
from time_utils import ts_utc_str


def _date_key(value) -> date | None:
    """Parse TMAD/SQLite dates defensively before ordering future sessions."""
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text, text[:10]]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
        except Exception:
            pass
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            pass
    return None


def _future_sessions(series: list[dict], observation_date: str, horizon: int) -> list[dict]:
    observation_key = _date_key(observation_date)
    if observation_key is None:
        return []
    future: list[tuple[date, dict]] = []
    for row in series:
        key = _date_key(row.get("trade_date"))
        if key is not None and key > observation_key:
            future.append((key, row))
    future.sort(key=lambda item: item[0])
    return [row for _key, row in future[:horizon]]


def materialize_outcomes(batch_size: int = 1000) -> int:
    """Label feature observations with bounded memory usage.

    The economic semantics are unchanged: each observation is labelled once a
    configured 1–5-session horizon is fully knowable. v6.2.56 only changes the
    execution shape: observations and pending outcome writes are processed in
    bounded keyset batches instead of materialising the full backlog and the
    full existing-outcome key set in Python memory.
    """
    prices = daily_price_series()
    generated_by_layer: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    completed_total = 0
    observations_reviewed = 0
    after_id = 0
    size = max(100, min(5000, int(batch_size)))

    while True:
        observations = observations_needing_labels_batch(
            after_id=after_id,
            batch_size=size,
        )
        if not observations:
            break

        after_id = int(observations[-1]["id"])
        observations_reviewed += len(observations)
        pending_rows: list[dict] = []

        for observation in observations:
            ticker = str(observation.get("ticker"))
            try:
                entry = float(observation.get("current_price"))
            except Exception:
                skipped["invalid_entry"] += 1
                continue
            if entry <= 0:
                skipped["invalid_entry"] += 1
                continue

            completed_horizons: set[int] = set()
            for raw in str(observation.get("completed_horizons") or "").split(","):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    completed_horizons.add(int(raw))
                except Exception:
                    continue

            series = prices.get(ticker) or []
            if not series:
                skipped["no_price_series"] += 1

            for horizon_value in config.OUTCOME_HORIZONS_SESSIONS:
                horizon = int(horizon_value)
                if horizon in completed_horizons:
                    continue
                window = _future_sessions(series, str(observation["snapshot_date"]), horizon)
                if len(window) < horizon:
                    skipped[f"insufficient_future_h{horizon}"] += 1
                    continue
                exit_row = window[-1]
                try:
                    exit_price = float(exit_row.get("close"))
                    highs = [
                        float(row.get("high") if row.get("high") is not None else row.get("close"))
                        for row in window
                    ]
                    lows = [
                        float(row.get("low") if row.get("low") is not None else row.get("close"))
                        for row in window
                    ]
                except Exception:
                    skipped["invalid_price_window"] += 1
                    continue

                ret = (exit_price / entry - 1.0) * 100.0
                mfe = max((value / entry - 1.0) * 100.0 for value in highs)
                mae = min((value / entry - 1.0) * 100.0 for value in lows)
                pending_rows.append({
                    "observation_id": observation["id"],
                    "ticker": ticker,
                    "observation_date": observation["snapshot_date"],
                    "horizon_sessions": horizon,
                    "entry_price": entry,
                    "first_session_date": window[0].get("trade_date"),
                    "exit_date": exit_row.get("trade_date"),
                    "exit_price": exit_price,
                    "return_long_pct": round(ret, 8),
                    "mfe_long_pct": round(mfe, 8),
                    "mae_long_pct": round(mae, 8),
                    "sessions_json": json.dumps([item.get("trade_date") for item in window]),
                    "complete": 1,
                    "labeled_at_utc": ts_utc_str(),
                    "unique_key": f"{observation['id']}|{horizon}",
                })
                generated_by_layer[str(observation.get("observation_layer") or "unknown")] += 1

        if pending_rows:
            completed_total += upsert_observation_outcomes_bulk(
                pending_rows,
                batch_size=size * max(1, len(set(config.OUTCOME_HORIZONS_SESSIONS))),
            )

    if completed_total:
        layer_text = ", ".join(
            f"{key}={value}" for key, value in sorted(generated_by_layer.items())
        ) or "none"
        skipped_text = ", ".join(
            f"{key}={value}" for key, value in sorted(skipped.items()) if value
        ) or "none"
        log_info(
            f"✅ [A06] Nuevos outcomes completados | rows={completed_total} | "
            f"observaciones revisadas={observations_reviewed} | "
            f"horizons={','.join(map(str, config.OUTCOME_HORIZONS_SESSIONS))} "
            f"| batch={size} | layers={layer_text} | skipped={skipped_text}",
            send_discord=False,
        )
    else:
        print(
            f"⏳ [A06] Sin nuevos outcomes | observaciones pendientes revisadas={observations_reviewed} "
            "| se necesitan sesiones futuras",
            flush=True,
        )
    return completed_total

