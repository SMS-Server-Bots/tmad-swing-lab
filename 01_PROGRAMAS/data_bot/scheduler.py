from config import SCANNER_CAPTURE_TIMES, GEX_CAPTURE_TIMES, RUN_WEEKENDS
from time_utils import now_madrid, session_label_from_time, is_operational_window


def should_run_today() -> bool:
    return is_operational_window(now_madrid())


def _minutes(hhmm: str) -> int:
    hour, minute = [int(x) for x in hhmm.split(":", 1)]
    return hour * 60 + minute


def _latest_due(job_type: str, times: list[str], already_run: set[str]):
    now = now_madrid()
    today = now.strftime("%Y-%m-%d")
    current = now.hour * 60 + now.minute
    due = []
    for hhmm in times:
        try:
            scheduled = _minutes(hhmm)
        except Exception:
            continue
        key = f"{today}|{job_type}|{hhmm}"
        if scheduled <= current and key not in already_run:
            due.append((scheduled, hhmm, key))
    if not due:
        return None
    due.sort()
    selected = due[-1]
    # A long capture must not trigger a cascade of obsolete hourly runs.
    for _scheduled, _hhmm, key in due[:-1]:
        already_run.add(key)
    return selected


def due_jobs(already_run: set[str]) -> list[tuple[str, str, str]]:
    """Return at most the latest pending job for each type.

    If a capture lasts longer than expected, old hourly slots are marked skipped and the
    newest due slot runs next. This keeps the bot current instead of replaying stale hours.
    """
    if not should_run_today():
        return []
    jobs = []
    scanner = _latest_due("scanner", SCANNER_CAPTURE_TIMES, already_run)
    if scanner:
        _mins, hhmm, _key = scanner
        jobs.append(("scanner", hhmm, session_label_from_time(hhmm)))
    gex = _latest_due("gex", GEX_CAPTURE_TIMES, already_run)
    if gex:
        _mins, hhmm, _key = gex
        jobs.append(("gex", hhmm, session_label_from_time(hhmm)))
    return jobs
