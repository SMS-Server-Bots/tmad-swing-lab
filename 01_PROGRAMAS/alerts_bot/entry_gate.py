from __future__ import annotations


def decide_entry_gate(
    *,
    pipeline_entry_mode: str,
    model_validated: bool,
    validated_delivery_enabled: bool,
    precalibration_shadow_enabled: bool,
    run_research_only: bool,
    market_window_open: bool,
) -> dict:
    """Return a fail-closed entry decision from the authoritative pipeline mode.

    A provisional/missing model must never manufacture SHADOW permission while
    the reconciler has explicitly set ``entry_mode=off``. Only SHADOW_READY may
    open the preliminary gate and only LIVE_READY + champion may open live.
    """
    mode = str(pipeline_entry_mode or "off").strip().lower()
    live_mode = bool(mode == "live" and model_validated and validated_delivery_enabled)
    shadow_mode = bool(mode == "shadow" and precalibration_shadow_enabled)
    entries_allowed = bool(
        not run_research_only
        and market_window_open
        and (live_mode or shadow_mode)
    )

    if run_research_only:
        reason = "research_or_deep_run"
    elif not market_window_open:
        reason = "market_window_closed"
    elif mode not in {"live", "shadow"}:
        reason = "pipeline_or_reconciliation_off"
    elif mode == "shadow" and not precalibration_shadow_enabled:
        reason = "shadow_delivery_not_enabled"
    elif shadow_mode:
        reason = "precalibration_shadow"
    elif live_mode:
        reason = "validated_champion"
    elif mode == "live" and not model_validated:
        reason = "live_model_not_champion"
    else:
        reason = "model_delivery_not_enabled"

    return {
        "pipeline_entry_mode": mode,
        "live_mode": live_mode,
        "shadow_mode": shadow_mode,
        "entries_allowed": entries_allowed,
        "reason": reason,
    }
