from __future__ import annotations

import csv
import json
import math
from statistics import mean
from typing import Any

import config
from asset_universe import (
    CORE_MARKET_SYMBOLS, relationship_priors, infer_structural_relationships,
    classify_asset,
)
from db import (
    daily_price_series,
    feature_rows_for_run,
    feature_runs_for_relational_backfill,
    get_asset_relationships,
    get_latest_ticker_metadata,
    insert_relational_context,
    previous_feature_map,
    update_feature_relational,
    upsert_asset_relationship_history,
    upsert_asset_relationships,
    latest_canonical_daily_dates,
    upsert_asset_universe_nodes,
    get_asset_universe_nodes,
)
from discord_utils import log_info
from relational_features import attach_relational_features, build_relational_context
from time_utils import market_session_date_str, ts_utc_str


def _returns(series: list[dict]) -> dict[str, float]:
    ordered = sorted(series, key=lambda row: str(row.get("trade_date") or ""))
    out: dict[str, float] = {}
    previous: float | None = None
    for row in ordered:
        try:
            close = float(row.get("close"))
        except Exception:
            continue
        if previous and previous > 0:
            out[str(row.get("trade_date"))] = close / previous - 1.0
        previous = close
    return out


def _returns_map(prices: dict[str, list[dict]]) -> dict[str, dict[str, float]]:
    return {ticker: _returns(rows) for ticker, rows in prices.items()}


def _as_of(values: dict[str, float], effective_date: str) -> dict[str, float]:
    return {day: value for day, value in values.items() if day <= effective_date}


def _moments(x: list[float], y: list[float]) -> tuple[float | None, float | None]:
    if len(x) < config.RELATIONSHIP_MIN_OBSERVATIONS or len(y) != len(x):
        return None, None
    mx, my = mean(x), mean(y)
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 1e-15 or vy <= 1e-15:
        return None, None
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    corr = cov / math.sqrt(vx * vy)
    beta = cov / vy
    return max(-1.0, min(1.0, corr)), beta


def _aligned(a: dict[str, float], b: dict[str, float], lookback: int) -> tuple[list[float], list[float]]:
    dates = sorted(set(a) & set(b))[-lookback:]
    return [a[d] for d in dates], [b[d] for d in dates]


def _lag_corr(ticker_returns: dict[str, float], related_returns: dict[str, float], lookback: int) -> float | None:
    related_dates = sorted(related_returns)
    previous_by_date = {
        related_dates[i]: related_returns[related_dates[i - 1]]
        for i in range(1, len(related_dates))
    }
    dates = sorted(set(ticker_returns) & set(previous_by_date))[-lookback:]
    x = [ticker_returns[d] for d in dates]
    y = [previous_by_date[d] for d in dates]
    corr, _beta = _moments(x, y)
    return corr


def _custom_relationship_rows(effective_date: str) -> list[dict]:
    path = config.CUSTOM_RELATIONSHIPS_FILE
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for item in csv.DictReader(handle):
                ticker = str(item.get("ticker") or "").upper().strip()
                related = str(item.get("related_ticker") or "").upper().strip()
                if not ticker or not related or ticker == related:
                    continue
                try:
                    base_weight = max(0.01, min(1.0, float(item.get("base_weight") or 0.5)))
                except Exception:
                    base_weight = 0.5
                try:
                    polarity = -1.0 if float(item.get("polarity") or 1.0) < 0 else 1.0
                except Exception:
                    polarity = 1.0
                rows.append({
                    "ticker": ticker,
                    "related_ticker": related,
                    "relation_type": str(item.get("relation_type") or "custom"),
                    "base_weight": base_weight,
                    "dynamic_weight": base_weight,
                    "correlation": -base_weight if polarity < 0 else None,
                    "beta": None,
                    "lead_lag_corr": None,
                    "sample_size": 0,
                    "effective_date": effective_date,
                    "updated_at_utc": ts_utc_str(),
                    "metadata_json": json.dumps(
                        {"source": "02_CONFIG/asset_relationships.csv", "note": item.get("note")},
                        ensure_ascii=False,
                    ),
                })
    except Exception:
        return []
    return rows


def _relationship_rows_as_of(
    effective_date: str,
    returns_by_ticker: dict[str, dict[str, float]],
    metadata: dict[str, dict],
) -> list[dict]:
    """Build a point-in-time graph from every Scanner node.

    The graph combines:
    - broad/sector priors;
    - structural edges inferred from current Scanner names/categories, including
      single-stock and leveraged/inverse ETFs;
    - point-in-time correlation, beta and lead-lag against relevant ETF/market
      anchors. No return after ``effective_date`` is visible.
    """
    rows: list[dict] = []
    all_tickers = sorted(set(returns_by_ticker) | set(metadata))
    universe = {
        ticker: {
            "name": (metadata.get(ticker) or {}).get("name"),
            "category": (metadata.get(ticker) or {}).get("category") or (metadata.get(ticker) or {}).get("sector"),
            "sector": (metadata.get(ticker) or {}).get("sector"),
            "industry": (metadata.get(ticker) or {}).get("industry"),
        }
        for ticker in all_tickers
    }
    structural = infer_structural_relationships(universe)
    structural_by_ticker: dict[str, list[dict]] = {}
    for item in structural:
        structural_by_ticker.setdefault(str(item["ticker"]), []).append(item)

    as_of_cache = {
        ticker: _as_of(values, effective_date)
        for ticker, values in returns_by_ticker.items()
    }
    available = set(all_tickers)
    for ticker in all_tickers:
        ticker_returns = as_of_cache.get(ticker, {})
        meta = metadata.get(ticker, {})
        candidate_specs: list[dict] = []
        seen: set[str] = set()

        # Structural Scanner-derived edges have first priority.
        for item in structural_by_ticker.get(ticker, []):
            related = str(item.get("related_ticker") or "")
            if related and related != ticker and related in available and related not in seen:
                candidate_specs.append({**item, "related": related})
                seen.add(related)

        # Static economic priors are fallback candidates, never proof.
        for rank, related in enumerate(relationship_priors(ticker, meta.get("sector"), meta.get("industry"))):
            if related == ticker or related not in available or related in seen:
                continue
            candidate_specs.append({
                "related": related,
                "relation_type": "ticker_etf" if related not in CORE_MARKET_SYMBOLS else "ticker_market",
                "base_weight": max(0.15, 0.75 - rank * 0.10),
                "polarity": 1.0,
                "metadata": {"source": "economic_prior"},
            })
            seen.add(related)

        # Every node is also tested against available market/ETF anchors. Keep
        # only the strongest few learned anchors to avoid a dense noisy graph.
        anchor_candidates = [
            symbol for symbol, anchor_meta in universe.items()
            if symbol != ticker and symbol not in seen and (
                symbol in CORE_MARKET_SYMBOLS
                or classify_asset(symbol, anchor_meta.get("name"), anchor_meta.get("category"))
                in {"etf", "leveraged_or_inverse_etf", "volatility_index", "index"}
            )
        ]
        learned_anchors: list[tuple[float, dict]] = []
        for related in anchor_candidates:
            related_returns = as_of_cache.get(related, {})
            x, y = _aligned(ticker_returns, related_returns, config.RELATIONSHIP_LOOKBACK_SESSIONS)
            corr, beta = _moments(x, y)
            lead_lag = _lag_corr(ticker_returns, related_returns, config.RELATIONSHIP_LOOKBACK_SESSIONS)
            strength = max(abs(corr or 0.0), abs(lead_lag or 0.0) * 0.90)
            if strength < config.RELATIONSHIP_DYNAMIC_MIN_STRENGTH:
                continue
            learned_anchors.append((strength, {
                "related": related,
                "relation_type": "dynamic_anchor",
                "base_weight": max(0.05, min(1.0, strength)),
                "polarity": -1.0 if (lead_lag if abs(lead_lag or 0.0) > abs(corr or 0.0) else corr or 0.0) < 0 else 1.0,
                "metadata": {"source": "full_scanner_dynamic_anchor"},
                "precomputed": (corr, beta, lead_lag, len(x)),
            }))
        learned_anchors.sort(key=lambda item: item[0], reverse=True)
        for _strength, item in learned_anchors[: config.RELATIONSHIP_MAX_DYNAMIC_EDGES_PER_NODE]:
            related = str(item["related"])
            if related not in seen:
                candidate_specs.append(item)
                seen.add(related)

        for rank, spec in enumerate(candidate_specs):
            related = str(spec.get("related") or "")
            if not related or related == ticker:
                continue
            related_returns = as_of_cache.get(related, {})
            if spec.get("precomputed"):
                corr, beta, lead_lag, sample_size = spec["precomputed"]
                x = [0.0] * int(sample_size)
            else:
                x, y = _aligned(ticker_returns, related_returns, config.RELATIONSHIP_LOOKBACK_SESSIONS)
                corr, beta = _moments(x, y)
                lead_lag = _lag_corr(ticker_returns, related_returns, config.RELATIONSHIP_LOOKBACK_SESSIONS)
            prior_weight = float(spec.get("base_weight") or max(0.15, 0.75 - rank * 0.10))
            polarity = -1.0 if float(spec.get("polarity") or 1.0) < 0 else 1.0
            learned_strength = max(abs(corr or 0.0), abs(lead_lag or 0.0) * 0.90)
            dynamic_weight = prior_weight if learned_strength <= 0 else max(0.05, min(1.0, learned_strength))
            # Until enough observations exist, preserve the structural polarity so
            # inverse ETFs can already act as a correctly signed confirmation.
            effective_corr = corr
            if effective_corr is None and spec.get("relation_type") in {
                "leveraged_parent", "leveraged_confirmation", "single_stock_parent", "single_stock_confirmation"
            }:
                effective_corr = polarity * prior_weight
            rows.append({
                "ticker": ticker,
                "related_ticker": related,
                "relation_type": str(spec.get("relation_type") or "dynamic_anchor"),
                "base_weight": prior_weight,
                "dynamic_weight": dynamic_weight,
                "correlation": effective_corr,
                "beta": beta,
                "lead_lag_corr": lead_lag,
                "sample_size": len(x),
                "effective_date": effective_date,
                "updated_at_utc": ts_utc_str(),
                "metadata_json": json.dumps({
                    "point_in_time": True,
                    "polarity_prior": polarity,
                    **(spec.get("metadata") or {}),
                }, ensure_ascii=False),
            })
    rows.extend(_custom_relationship_rows(effective_date))
    return rows

def _relationship_map(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row.get("ticker") or ""), []).append(row)
    for ticker in out:
        out[ticker].sort(
            key=lambda row: float(row.get("dynamic_weight") or row.get("base_weight") or 0.0),
            reverse=True,
        )
    return out


def _export_graph_catalog(nodes: dict[str, dict], rows: list[dict]) -> dict[str, str]:
    """Write a human-auditable graph catalogue without secrets."""
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    nodes_path = config.OUTPUT_DIR / "asset_graph_nodes.csv"
    edges_path = config.OUTPUT_DIR / "asset_graph_edges.csv"
    with nodes_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["ticker", "name", "category", "sector", "industry", "asset_type", "first_seen_date", "last_seen_date"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for ticker, item in sorted(nodes.items()):
            writer.writerow({field: (ticker if field == "ticker" else item.get(field)) for field in fields})
    with edges_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "ticker", "related_ticker", "relation_type", "base_weight", "dynamic_weight",
            "correlation", "beta", "lead_lag_corr", "sample_size", "effective_date", "polarity_prior", "source",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(rows, key=lambda item: (str(item.get("ticker")), str(item.get("relation_type")), str(item.get("related_ticker")))):
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except Exception:
                metadata = {}
            writer.writerow({
                "ticker": row.get("ticker"),
                "related_ticker": row.get("related_ticker"),
                "relation_type": row.get("relation_type"),
                "base_weight": row.get("base_weight"),
                "dynamic_weight": row.get("dynamic_weight"),
                "correlation": row.get("correlation"),
                "beta": row.get("beta"),
                "lead_lag_corr": row.get("lead_lag_corr"),
                "sample_size": row.get("sample_size"),
                "effective_date": row.get("effective_date"),
                "polarity_prior": metadata.get("polarity_prior"),
                "source": metadata.get("source"),
            })
    return {"nodes_csv": str(nodes_path), "edges_csv": str(edges_path)}


def refresh_relationship_graph(force: bool = False, effective_date: str | None = None) -> dict[str, Any]:
    """Update the graph as of the latest observable canonical close.

    Before the US close, using today's calendar date made the diagnostics look as
    if the graph had seen a close that did not exist. The effective date now comes
    from the latest canonical deep close unless explicitly supplied.
    """
    prices = daily_price_series()
    metadata = get_latest_ticker_metadata()
    persisted_nodes = get_asset_universe_nodes()
    for ticker, item in persisted_nodes.items():
        metadata.setdefault(ticker, {}).update({
            key: value for key, value in item.items()
            if key in {"name", "category", "sector", "industry", "asset_type"} and value is not None
        })
    returns_by_ticker = _returns_map(prices)
    canonical = latest_canonical_daily_dates(1)
    today = effective_date or (canonical[-1] if canonical else market_session_date_str())
    node_rows = [
        {
            "ticker": ticker,
            "name": item.get("name"),
            "category": item.get("category") or item.get("sector"),
            "sector": item.get("sector") or item.get("category"),
            "industry": item.get("industry"),
            "asset_type": classify_asset(ticker, item.get("name"), item.get("category") or item.get("sector")),
            "last_seen_date": today,
            "metadata": {"source": "full_scanner_or_core_history"},
        }
        for ticker, item in metadata.items()
    ]
    nodes_inserted = upsert_asset_universe_nodes(node_rows)
    rows = _relationship_rows_as_of(today, returns_by_ticker, metadata)
    inserted = upsert_asset_relationships(rows)
    history_rows = upsert_asset_relationship_history(rows)
    node_catalog = get_asset_universe_nodes()
    exports = _export_graph_catalog(node_catalog, rows)
    custom_count = sum(1 for row in rows if str(row.get("relation_type") or "").startswith("custom"))
    summary = {
        "effective_date": today,
        "nodes": nodes_inserted,
        "relationships": inserted,
        "history_relationships": history_rows,
        "tickers": len({row["ticker"] for row in rows}),
        "with_correlation": sum(1 for row in rows if row.get("correlation") is not None),
        "custom_relationships": custom_count,
        "lookback_sessions": config.RELATIONSHIP_LOOKBACK_SESSIONS,
        "point_in_time": True,
        "structural_edges": sum(
            1 for row in rows
            if str(row.get("relation_type") or "") in {
                "leveraged_parent", "leveraged_confirmation",
                "single_stock_parent", "single_stock_confirmation",
            }
        ),
        **exports,
    }
    log_info(
        "🕸️ Grafo relacional actualizado | "
        f"date={today} | nodes={summary['nodes']} | tickers={summary['tickers']} | edges={summary['relationships']} | "
        f"dynamic={summary['with_correlation']} | lookback={summary['lookback_sessions']} | PIT=true",
        send_discord=False,
    )
    return summary


def backfill_relational_feature_history(limit_runs: int = 1000) -> dict[str, Any]:
    """Rebuild historical graph features chronologically without future leakage.

    Weekly ZIPs only need raw Scanner/GEX/price observations. Derived market,
    sector and relationship variables are recreated from data that existed at
    each historical date and saved beside the feature observation.
    """
    run_ids = feature_runs_for_relational_backfill(limit_runs)
    if not run_ids:
        return {"runs": 0, "observations": 0, "contexts": 0, "graph_dates": 0, "point_in_time": True}

    prices = daily_price_series()
    returns_by_ticker = _returns_map(prices)
    latest_metadata = get_latest_ticker_metadata()
    previous: dict[str, dict] = {}
    graph_cache: dict[str, dict[str, list[dict]]] = {}
    updated = 0
    contexts = 0

    for run_id in run_ids:
        rows = feature_rows_for_run(run_id)
        current: dict[str, dict] = {}
        parsed: dict[str, tuple[int, dict, dict]] = {}
        run_metadata = dict(latest_metadata)
        snapshot_date = ""
        for row in rows:
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            snapshot_date = snapshot_date or str(row.get("snapshot_date") or "")
            try:
                features = json.loads(row.get("features_json") or "{}")
            except Exception:
                features = {}
            current[ticker] = features
            parsed[ticker] = (int(row.get("id") or 0), row, features)
            run_metadata[ticker] = {
                "sector": features.get("sector"),
                "industry": features.get("industry"),
                "sector_etf": features.get("sector_etf"),
            }
        if not current or not snapshot_date:
            continue

        relationship_map = graph_cache.get(snapshot_date)
        if relationship_map is None:
            graph_rows = _relationship_rows_as_of(snapshot_date, returns_by_ticker, run_metadata)
            upsert_asset_relationship_history(graph_rows)
            relationship_map = _relationship_map(graph_rows)
            graph_cache[snapshot_date] = relationship_map

        prior_state = previous or previous_feature_map(run_id)
        relational_by_ticker, _summary = build_relational_context(
            current,
            previous_by_ticker=prior_state,
            relationship_map=relationship_map,
        )
        for ticker, (observation_id, row, features) in parsed.items():
            relational = relational_by_ticker.get(ticker)
            if not relational:
                continue
            enriched = attach_relational_features(features, relational)
            update_feature_relational(observation_id, enriched, relational)
            insert_relational_context({
                "source_scanner_run_id": run_id,
                "snapshot_date": row.get("snapshot_date"),
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
                "context_json": json.dumps(relational, ensure_ascii=False),
                "unique_key": f"{run_id}|{ticker}",
            })
            updated += 1
            contexts += 1
        previous = {
            ticker: attach_relational_features(features, relational_by_ticker.get(ticker))
            for ticker, features in current.items()
        }

    summary = {
        "runs": len(run_ids),
        "observations": updated,
        "contexts": contexts,
        "graph_dates": len(graph_cache),
        "point_in_time": True,
    }
    log_info(
        f"🧬 Backfill relacional PIT | runs={len(run_ids)} | observations={updated} "
        f"| graph_dates={len(graph_cache)} | future_leakage=false",
        send_discord=False,
    )
    return summary
