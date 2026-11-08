from __future__ import annotations

import config


def build_core_calibration_signature(
    *, universe_fingerprint: str, excluded_fingerprint: str = "",
    window_fingerprint: str = "", end_date: str = "",
    feature_dates: int = 0, outcome_dates: int = 0, outcome_rows: int = 0,
) -> str:
    """Versioned idempotency key for one scientific CORE state."""
    return "|".join([
        str(config.BOT_VERSION),
        str(getattr(config, "CORE_CALIBRATION_ENGINE_SIGNATURE", "core")),
        str(universe_fingerprint or ""),
        str(excluded_fingerprint or ""),
        str(window_fingerprint or ""),
        str(end_date or ""),
        str(int(feature_dates or 0)),
        str(int(outcome_dates or 0)),
        str(int(outcome_rows or 0)),
    ])
