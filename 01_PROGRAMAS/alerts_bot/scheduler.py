import config
from time_utils import now_madrid, is_operational_window


def _minutes(hhmm: str) -> int:
    hour, minute = [int(x) for x in hhmm.split(":", 1)]
    return hour * 60 + minute


def due_jobs(already_run: set[str]):
    now = now_madrid()
    if not is_operational_window(now):
        return []
    today = now.strftime("%Y-%m-%d")
    current = now.hour * 60 + now.minute
    mapping = [
        ("alerts", config.ALERT_CHECK_TIMES),
        ("premarket_setup", config.PREMARKET_SETUP_TIMES),
        ("labels", config.LABEL_TIMES),
        ("train", config.TRAIN_TIMES),
        ("export", config.EXPORT_TIMES),
    ]
    jobs = []
    for job_type, times in mapping:
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
            continue
        due.sort()
        selected = due[-1]
        for _scheduled, _hhmm, key in due[:-1]:
            already_run.add(key)
        jobs.append((job_type, selected[1]))
    return jobs
