"""Manual backtest entry point with persistent reports and Discord status."""
from datetime import datetime

from outcomes import materialize_outcomes
from optimizer import run_optimizer
from performance_report import build_performance_report


def run_backtest():
    labeled = materialize_outcomes()
    run_optimizer()
    report = build_performance_report(f"manual_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    return {"labeled": labeled, "summary": report.get("summary")}
