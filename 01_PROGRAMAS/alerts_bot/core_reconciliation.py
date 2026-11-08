from __future__ import annotations

from typing import Any


def _as_ticker_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item or "").strip().upper() for item in value if str(item or "").strip()}


def reconcile_core_universe(
    manifest: dict,
    materialization: dict,
    *,
    feature_tickers: int = 0,
) -> dict:
    """Reconcile Data's authoritative universe with Alerts' local materialisation.

    Data owns the current Scanner universe and the exclusion decisions. Alerts owns
    the feature cache. A local exclusion checkpoint can lag after an update/restart;
    when the exact pending tickers match Data's excluded sample (or counts are fully
    authoritative when samples are unavailable), Alerts may calibrate in SHADOW.
    Such a reconciliation is never live-safe until local fingerprints converge.
    """
    source_count = int(materialization.get("universe_tickers") or 0)
    manifest_count = int(manifest.get("core_history_universe_tickers") or 0)
    source_complete_flag = bool(materialization.get("universe_complete"))
    manifest_complete_flag = bool(manifest.get("core_history_universe_complete"))
    source_fingerprint = str(materialization.get("universe_fingerprint") or "")
    manifest_fingerprint = str(manifest.get("core_history_universe_fingerprint") or "")

    source_complete_tickers = int(materialization.get("complete_tickers") or 0)
    manifest_complete_tickers = int(manifest.get("core_history_complete_tickers") or 0)
    feature_tickers = max(0, int(feature_tickers or 0))
    feature_ticker_shortfall = max(0, manifest_complete_tickers - feature_tickers)
    source_excluded = int(materialization.get("excluded_tickers") or 0)
    manifest_excluded = int(manifest.get("core_history_excluded_tickers") or 0)
    source_pending = int(
        materialization.get("pending_tickers", materialization.get("remaining_tickers", 0)) or 0
    )
    manifest_pending = int(
        manifest.get("core_history_pending_tickers", manifest.get("core_history_remaining_tickers", 0)) or 0
    )
    source_excluded_fingerprint = str(materialization.get("excluded_fingerprint") or "")
    manifest_excluded_fingerprint = str(manifest.get("core_history_excluded_fingerprint") or "")

    counts_match = bool(source_count > 0 and source_count == manifest_count)
    fingerprint_match = bool(source_fingerprint and source_fingerprint == manifest_fingerprint)
    exclusions_match = bool(
        source_excluded == manifest_excluded
        and source_excluded_fingerprint == manifest_excluded_fingerprint
    )
    strict_complete = bool(
        source_complete_flag
        and manifest_complete_flag
        and counts_match
        and fingerprint_match
        and exclusions_match
        and source_pending == 0
        and manifest_pending == 0
    )

    source_pending_sample = _as_ticker_set(
        materialization.get("pending_ticker_sample") or materialization.get("missing_ticker_sample")
    )
    manifest_excluded_sample = _as_ticker_set(manifest.get("core_history_excluded_ticker_sample"))
    samples_available = bool(source_pending_sample or manifest_excluded_sample)
    samples_match = bool(
        source_pending_sample
        and manifest_excluded_sample
        and source_pending_sample == manifest_excluded_sample
    )
    # Both lists are capped at 50. When the authoritative exclusion count is small,
    # exact sample equality is expected; if old manifests lack samples, the signed
    # universe/exclusion fingerprints and exact counts remain the fallback.
    sample_gate = bool(samples_match if manifest_excluded <= 50 and samples_available else True)

    authoritative_reconciliation = bool(
        manifest_complete_flag
        and manifest_count > 0
        and manifest_complete_tickers + manifest_excluded == manifest_count
        and manifest_pending == 0
        and counts_match
        and fingerprint_match
        and source_complete_tickers == manifest_complete_tickers
        and source_pending == manifest_excluded
        and feature_tickers >= manifest_complete_tickers
        and sample_gate
    )

    complete = bool(strict_complete or authoritative_reconciliation)
    warning = bool(authoritative_reconciliation and not strict_complete)
    coherent = bool(
        strict_complete
        or authoritative_reconciliation
        or (
            counts_match
            and fingerprint_match
            and exclusions_match
            and source_complete_flag == manifest_complete_flag
        )
    )

    effective_excluded = manifest_excluded if authoritative_reconciliation else source_excluded
    effective_pending = 0 if authoritative_reconciliation else source_pending
    effective_excluded_fingerprint = (
        manifest_excluded_fingerprint if authoritative_reconciliation else source_excluded_fingerprint
    )

    return {
        "complete": complete,
        "strict_complete": strict_complete,
        "coherent": coherent,
        "warning": warning,
        "live_safe": bool(strict_complete and not warning),
        "authoritative_reconciliation": authoritative_reconciliation,
        "source_complete": source_complete_flag,
        "manifest_complete": manifest_complete_flag,
        "source_count": source_count,
        "manifest_count": manifest_count,
        "complete_tickers": source_complete_tickers,
        "manifest_complete_tickers": manifest_complete_tickers,
        "feature_tickers": feature_tickers,
        "feature_ticker_shortfall": feature_ticker_shortfall,
        "resolved_tickers": source_complete_tickers + effective_excluded,
        "excluded_tickers": effective_excluded,
        "source_excluded_tickers": source_excluded,
        "manifest_excluded_tickers": manifest_excluded,
        "pending_tickers": effective_pending,
        "source_pending_tickers": source_pending,
        "manifest_pending_tickers": manifest_pending,
        "remaining_tickers": effective_pending,
        "counts_match": counts_match,
        "fingerprint_match": fingerprint_match,
        "exclusions_match": exclusions_match,
        "samples_available": samples_available,
        "samples_match": samples_match,
        "fingerprint": manifest_fingerprint if authoritative_reconciliation else source_fingerprint,
        "excluded_fingerprint": effective_excluded_fingerprint,
        "pending_ticker_sample": sorted(source_pending_sample)[:50],
        "excluded_ticker_sample": sorted(manifest_excluded_sample if authoritative_reconciliation else _as_ticker_set(materialization.get("excluded_ticker_sample")))[:50],
    }


def reconcile_core_dates(
    *,
    source_dates: int,
    feature_dates: int,
    target_dates: int,
    source_end_date: str | None,
    feature_end_date: str | None,
    errors: int = 0,
    nonfatal_limit: int = 0,
    source_date_values: list[str] | tuple[str, ...] | None = None,
    feature_date_values: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Reconcile CORE using real market-session membership whenever available.

    Counts and MAX(date) are only metadata. They can differ because a local cache
    contains harmless extra administrative dates. The safe SHADOW criterion is
    that every session in the latest required source window is actually present
    in SQLite and the source's latest market date is present. LIVE still requires
    an exact calendar match and zero materialisation errors.
    """
    source_dates = int(source_dates or 0)
    feature_dates = int(feature_dates or 0)
    target_dates = max(1, int(target_dates or 1))
    errors = int(errors or 0)
    nonfatal_limit = max(0, int(nonfatal_limit or 0))

    source_values = sorted({str(x) for x in (source_date_values or []) if str(x)})
    feature_values = sorted({str(x) for x in (feature_date_values or []) if str(x)})

    if source_values and feature_values:
        source_set = set(source_values)
        feature_set = set(feature_values)
        required_source = source_values[-target_dates:]
        missing_required = [day for day in required_source if day not in feature_set]
        extra_feature = sorted(feature_set - source_set)
        missing_any = sorted(source_set - feature_set)
        source_latest = source_values[-1]
        source_end_present = source_latest in feature_set
        exact = source_set == feature_set
        required_window_complete = bool(
            len(required_source) >= target_dates
            and source_end_present
            and not missing_required
        )
        complete = bool(errors <= nonfatal_limit and required_window_complete)
        warning = bool(complete and not exact)
        return {
            "complete": complete,
            "strict_complete": bool(errors == 0 and exact),
            "warning": warning,
            "live_safe": bool(errors == 0 and exact),
            "exact": exact,
            "equivalent_window": bool(complete and not exact),
            "membership_mode": True,
            "required_window_complete": required_window_complete,
            "source_dates": len(source_values),
            "feature_dates": len(feature_values),
            "target_dates": target_dates,
            "source_end_date": source_latest,
            "feature_end_date": feature_values[-1],
            "end_match": source_end_present,
            "drift": abs(len(source_values) - len(feature_values)),
            "max_drift": None,
            "missing_required_dates": missing_required[:20],
            "missing_source_dates": missing_any[:20],
            "extra_feature_dates": extra_feature[:20],
            "required_source_start": required_source[0] if required_source else "",
            "required_source_end": required_source[-1] if required_source else "",
            "errors": errors,
            "nonfatal_limit": nonfatal_limit,
        }

    # Compatibility fallback for old validators or unavailable date inventories.
    exact = source_dates == feature_dates
    end_match = bool(source_end_date and feature_end_date and str(source_end_date) == str(feature_end_date))
    drift = abs(source_dates - feature_dates)
    max_drift = max(2, min(10, target_dates // 5 or 2))
    equivalent_window = bool(
        source_dates >= target_dates
        and feature_dates >= target_dates
        and end_match
        and drift <= max_drift
    )
    complete = bool(errors <= nonfatal_limit and (exact or equivalent_window))
    warning = bool(complete and not exact)
    return {
        "complete": complete,
        "strict_complete": bool(errors == 0 and exact),
        "warning": warning,
        "live_safe": bool(errors == 0 and exact),
        "exact": exact,
        "equivalent_window": equivalent_window,
        "membership_mode": False,
        "source_dates": source_dates,
        "feature_dates": feature_dates,
        "target_dates": target_dates,
        "source_end_date": str(source_end_date or ""),
        "feature_end_date": str(feature_end_date or ""),
        "end_match": end_match,
        "drift": drift,
        "max_drift": max_drift,
        "missing_required_dates": [],
        "missing_source_dates": [],
        "extra_feature_dates": [],
        "errors": errors,
        "nonfatal_limit": nonfatal_limit,
    }

