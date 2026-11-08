from __future__ import annotations

"""Synthetic option repricing for TMAD EOD SHADOW selections.

This module deliberately does NOT manufacture market quotes.  It creates a
model-based scenario proxy for the same CALL/PUT contract referenced by TMAD,
holding volatility constant and repricing the contract after the realised
underlying move at 1/2/3/5 sessions.

The result is research evidence only:
- real option PnL remains unavailable until actual option marks exist;
- no LIVE signal, threshold, size, stop or target is changed;
- future prices are used only after an outcome is mature.
"""

import json
import math
from datetime import date
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

HORIZONS = (1, 2, 3, 5)
MIN_THEORETICAL_PREMIUM = 0.10
DEFAULT_ANNUAL_VOL_PCT = 45.0
MIN_ANNUAL_VOL_PCT = 10.0
MAX_ANNUAL_VOL_PCT = 250.0


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except Exception:
        return None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(
    instrument: str,
    spot: float,
    strike: float,
    dte_days: float,
    annual_vol_pct: float,
    *,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> float | None:
    """European Black-Scholes model price used only as a relative SHADOW proxy."""
    kind = str(instrument or "").upper()
    s = _f(spot)
    k = _f(strike)
    vol_pct = _f(annual_vol_pct)
    days = _f(dte_days)
    if kind not in {"CALL", "PUT"} or not s or not k or s <= 0 or k <= 0 or vol_pct is None or vol_pct <= 0 or days is None:
        return None

    if days <= 0:
        return max(0.0, s - k) if kind == "CALL" else max(0.0, k - s)

    t = max(days / 365.0, 1.0 / 3650.0)
    sigma = max(vol_pct / 100.0, 1e-8)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(s / k) + (risk_free_rate - dividend_yield + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc_r = math.exp(-risk_free_rate * t)
    disc_q = math.exp(-dividend_yield * t)
    if kind == "CALL":
        value = s * disc_q * _norm_cdf(d1) - k * disc_r * _norm_cdf(d2)
    else:
        value = k * disc_r * _norm_cdf(-d2) - s * disc_q * _norm_cdf(-d1)
    return max(0.0, float(value))


def _annualize_daily_vol_pct(daily_pct: float) -> float:
    return abs(float(daily_pct)) * math.sqrt(252.0)


def _clip_vol(value: float) -> float:
    return max(MIN_ANNUAL_VOL_PCT, min(MAX_ANNUAL_VOL_PCT, float(value)))


def _feature_payload(row: dict | None) -> dict:
    if not row:
        return {}
    payload = _json(row.get("features_json"), {})
    return payload if isinstance(payload, dict) else {}


def _vol_from_features(features: dict) -> tuple[float | None, str | None, float | None]:
    realized = _f(features.get("realized_volatility_20d"))
    if realized is not None and realized > 0:
        # TMAD stores realised volatility as a daily percentage (e.g. ~2-4).
        annual = _annualize_daily_vol_pct(realized) if realized <= 15.0 else realized
        return _clip_vol(annual), "REALIZED_VOLATILITY_20D", realized

    atr = _f(features.get("atr_pct"))
    if atr is not None and atr > 0:
        # ATR is a range, not sigma.  Dividing by ~1.6 is a transparent rough
        # range-to-sigma conversion before annualising.
        daily_sigma_pct = atr / 1.6
        return _clip_vol(_annualize_daily_vol_pct(daily_sigma_pct)), "ATR_RANGE_TO_SIGMA_PROXY", atr
    return None, None, None


def _point_in_time_volatility(
    journal_row: dict,
    feature_row: dict | None,
    prior_one_day_returns: list[float],
) -> dict:
    supplied = journal_row.get("volatility_proxy_input")
    if isinstance(supplied, dict):
        source = str(supplied.get("source") or "").upper()
        value = _f(supplied.get("daily_pct") or supplied.get("value"))
        if value is not None and value > 0:
            if "ATR" in source:
                annual = _annualize_daily_vol_pct(value / 1.6)
            else:
                annual = _annualize_daily_vol_pct(value) if value <= 15.0 else value
            return {
                "annual_vol_pct": round(_clip_vol(annual), 4),
                "source": source or "JOURNALED_POINT_IN_TIME",
                "raw_value": value,
                "confidence": "POINT_IN_TIME",
            }

    annual, source, raw = _vol_from_features(_feature_payload(feature_row))
    if annual is not None:
        return {
            "annual_vol_pct": round(annual, 4),
            "source": source,
            "raw_value": raw,
            "confidence": "POINT_IN_TIME_FALLBACK",
        }

    clean = [float(x) for x in prior_one_day_returns if _f(x) is not None]
    if len(clean) >= 8:
        annual = _clip_vol(stdev(clean) * math.sqrt(252.0))
        return {
            "annual_vol_pct": round(annual, 4),
            "source": "PRIOR_1D_CLOSE_RETURNS",
            "raw_value": round(stdev(clean), 6),
            "confidence": "HISTORICAL_PRE_SIGNAL",
            "sample": len(clean),
        }

    return {
        "annual_vol_pct": DEFAULT_ANNUAL_VOL_PCT,
        "source": "DEFAULT_ASSUMPTION",
        "raw_value": None,
        "confidence": "LOW",
        "sample": len(clean),
    }


def _profit_factor(values: list[float]) -> float | None:
    positives = [x for x in values if x > 0]
    negatives = [x for x in values if x < 0]
    if not negatives or not sum(negatives):
        return None
    return sum(positives) / abs(sum(negatives))


def _metrics(rows: list[dict]) -> dict:
    values = [float(row["synthetic_option_return_pct"]) for row in rows if _f(row.get("synthetic_option_return_pct")) is not None]
    underlying = [float(row["underlying_directional_return_pct"]) for row in rows if _f(row.get("underlying_directional_return_pct")) is not None]
    positives = [x for x in values if x > 0]
    return {
        "sample": len(values),
        "win_rate_pct": round(100.0 * len(positives) / len(values), 2) if values else None,
        "avg_synthetic_option_return_pct": round(mean(values), 4) if values else None,
        "median_synthetic_option_return_pct": round(median(values), 4) if values else None,
        "profit_factor": round(_profit_factor(values), 4) if _profit_factor(values) is not None else None,
        "avg_underlying_directional_return_pct": round(mean(underlying), 4) if underlying else None,
        "median_underlying_directional_return_pct": round(median(underlying), 4) if underlying else None,
    }


def _journal_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        key = str(row.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def evaluate_synthetic_option_proxy(journal_path: str | Path, feature_rows: list[dict]) -> dict:
    """Reprice journalled EOD contracts against mature underlying outcomes."""
    path = Path(journal_path)
    journal = _journal_rows(path)
    if not journal:
        return {
            "status": "WAITING_FOR_EOD_SELECTIONS",
            "contracts_journaled": 0,
            "evaluated_contract_horizons": 0,
            "by_horizon": {str(h): _metrics([]) for h in HORIZONS},
            "policy": _policy(),
        }

    # Point-in-time feature row and mature outcomes indexed by ticker/date/horizon.
    feature_index: dict[tuple[str, str], dict] = {}
    outcomes: dict[tuple[str, str, int], dict] = {}
    prior_1d: dict[str, list[dict]] = {}
    outcome_dates: set[str] = set()
    for row in feature_rows:
        ticker = str(row.get("ticker") or "").upper()
        signal_date = str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]
        if not ticker or not signal_date:
            continue

        fkey = (ticker, signal_date)
        old = feature_index.get(fkey)
        rank = (
            int(row.get("is_canonical_daily_close") or 0),
            -int(row.get("observation_id") or 10**18),
        )
        old_rank = (
            int((old or {}).get("is_canonical_daily_close") or 0),
            -int((old or {}).get("observation_id") or 10**18),
        )
        if old is None or rank > old_rank:
            feature_index[fkey] = row

        horizon = int(row.get("horizon_sessions") or 0)
        complete = int(row.get("complete") or 0) if row.get("complete") is not None else 1
        if horizon in HORIZONS and complete == 1 and _f(row.get("exit_price")) is not None:
            okey = (ticker, signal_date, horizon)
            previous = outcomes.get(okey)
            obs_id = int(row.get("observation_id") or 10**18)
            if previous is None or obs_id < int(previous.get("observation_id") or 10**18):
                outcomes[okey] = row
            outcome_dates.add(signal_date)
        if horizon == 1 and complete == 1 and _f(row.get("return_long_pct")) is not None:
            prior_1d.setdefault(ticker, []).append(row)

    evaluated: list[dict] = []
    skipped: dict[str, int] = {}
    journal_dates = sorted({str(item.get("asof") or "")[:10] for item in journal if str(item.get("asof") or "")[:10]})
    contracts_with_any_outcome: set[str] = set()
    contracts_without_any_outcome: set[str] = set()
    for item in journal:
        ticker = str(item.get("ticker") or "").upper()
        asof = str(item.get("asof") or "")[:10]
        direction = str(item.get("direction") or "").upper()
        spot0 = _f(item.get("spot"))
        expression = item.get("option_expression") if isinstance(item.get("option_expression"), dict) else {}
        contract = item.get("contract_reference") if isinstance(item.get("contract_reference"), dict) else {}
        instrument = str(contract.get("instrument") or expression.get("instrument") or ("CALL" if direction == "LONG" else "PUT")).upper()
        strike = _f(contract.get("strike") or expression.get("strike"))
        expiry_text = str(contract.get("expiry") or expression.get("expiry") or "")[:10]
        expiry = _date(expiry_text)
        signal_day = _date(asof)
        if not ticker or direction not in {"LONG", "SHORT"} or not spot0 or not strike or not expiry or not signal_day:
            skipped["INVALID_CONTRACT_REFERENCE"] = skipped.get("INVALID_CONTRACT_REFERENCE", 0) + 1
            continue
        entry_dte = max(0, (expiry - signal_day).days)

        prior_returns = []
        for prior in prior_1d.get(ticker, []):
            exit_day = _date(prior.get("exit_date"))
            if exit_day and exit_day <= signal_day:
                value = _f(prior.get("return_long_pct"))
                if value is not None:
                    prior_returns.append(value)
        vol = _point_in_time_volatility(item, feature_index.get((ticker, asof)), prior_returns)
        annual_vol = float(vol["annual_vol_pct"])
        entry_price = black_scholes_price(instrument, spot0, strike, entry_dte, annual_vol)
        if entry_price is None:
            skipped["MODEL_INPUT_UNAVAILABLE"] = skipped.get("MODEL_INPUT_UNAVAILABLE", 0) + 1
            continue
        if entry_price < MIN_THEORETICAL_PREMIUM:
            skipped["LOW_THEORETICAL_ENTRY_PREMIUM"] = skipped.get("LOW_THEORETICAL_ENTRY_PREMIUM", 0) + 1
            continue

        matched_this_contract = False
        for horizon in HORIZONS:
            outcome = outcomes.get((ticker, asof, horizon))
            if not outcome:
                continue
            matched_this_contract = True
            spot_h = _f(outcome.get("exit_price"))
            exit_day = _date(outcome.get("exit_date"))
            if not spot_h or not exit_day:
                continue
            remaining_dte = max(0, (expiry - exit_day).days)
            exit_price = black_scholes_price(instrument, spot_h, strike, remaining_dte, annual_vol)
            if exit_price is None:
                continue
            option_return = (exit_price / entry_price - 1.0) * 100.0
            underlying_return = (
                (spot_h / spot0 - 1.0) * 100.0
                if direction == "LONG"
                else (spot0 / spot_h - 1.0) * 100.0
            )
            evaluated.append({
                "key": item.get("key"),
                "asof": asof,
                "ticker": ticker,
                "direction": direction,
                "instrument": instrument,
                "strike": round(strike, 4),
                "expiry": expiry_text,
                "horizon_sessions": horizon,
                "entry_spot": round(spot0, 6),
                "exit_spot": round(spot_h, 6),
                "entry_dte_calendar_days": entry_dte,
                "remaining_dte_calendar_days": remaining_dte,
                "annual_vol_pct": round(annual_vol, 4),
                "volatility_source": vol.get("source"),
                "volatility_confidence": vol.get("confidence"),
                "entry_theoretical_option_price": round(entry_price, 6),
                "exit_theoretical_option_price": round(exit_price, 6),
                "synthetic_option_return_pct": round(option_return, 4),
                "underlying_directional_return_pct": round(underlying_return, 4),
            })
        contract_key = str(item.get("key") or f"{asof}|{ticker}|{direction}")
        if matched_this_contract:
            contracts_with_any_outcome.add(contract_key)
        else:
            contracts_without_any_outcome.add(contract_key)
            skipped["NO_MATURE_OUTCOME_MATCH"] = skipped.get("NO_MATURE_OUTCOME_MATCH", 0) + 1

    by_horizon = {
        str(h): _metrics([row for row in evaluated if int(row.get("horizon_sessions") or 0) == h])
        for h in HORIZONS
    }
    status = "EVALUABLE" if evaluated else "WAITING_FOR_MATURE_EOD_SELECTIONS"
    sources: dict[str, int] = {}
    for row in evaluated:
        source = str(row.get("volatility_source") or "UNKNOWN")
        sources[source] = sources.get(source, 0) + 1
    return {
        "schema_version": 1,
        "mode": "SYNTHETIC_OPTION_PROXY",
        "status": status,
        "contracts_journaled": len(journal),
        "evaluated_contract_horizons": len(evaluated),
        "volatility_sources": sources,
        "skipped": skipped,
        "maturity_diagnostics": {
            "journal_date_min": journal_dates[0] if journal_dates else None,
            "journal_date_max": journal_dates[-1] if journal_dates else None,
            "outcome_signal_date_min": min(outcome_dates) if outcome_dates else None,
            "outcome_signal_date_max": max(outcome_dates) if outcome_dates else None,
            "outcome_rows_indexed": len(outcomes),
            "contracts_with_any_mature_outcome": len(contracts_with_any_outcome),
            "contracts_without_any_mature_outcome": len(contracts_without_any_outcome),
            "note": "A journalled contract is evaluable only when a mature future-session underlying outcome exists for the same ticker, signal date and horizon.",
        },
        "by_horizon": by_horizon,
        "sample_rows": evaluated[-20:],
        "policy": _policy(),
    }


def _policy() -> dict:
    return {
        "shadow_research_only": True,
        "real_option_pnl": False,
        "real_option_marks_untouched": True,
        "future_prices_used_only_after_maturity": True,
        "same_contract_strike_and_expiry_repriced": True,
        "constant_volatility_over_horizon": True,
        "risk_free_rate": 0.0,
        "dividend_yield": 0.0,
        "black_scholes_european_proxy": True,
        "not_execution_quality": True,
        "not_a_substitute_for_bid_ask_iv_or_greeks": True,
        "horizon_panels_are_not_independent_trades": True,
    }
