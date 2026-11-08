from __future__ import annotations

import json
import traceback

from db import ensure_alerts_schema
from discord_utils import log_error, log_info
from performance_report import build_performance_report
from research import export_research_tables
from startup_calibration import run_startup_warmup


def main():
    ensure_alerts_schema()
    try:
        startup = run_startup_warmup()
        export_research_tables()
        report = build_performance_report("initial_baseline")
        result = {"startup": startup, "report": report.get("summary")}
        log_info(f"✅ Primera ejecución completada | {json.dumps(result, ensure_ascii=False, default=str)}", send_discord=False)
        return 0
    except Exception as exc:
        log_error(f"🟥 Primera ejecución incompleta: {exc}\n```{traceback.format_exc()[-1800:]}```")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
