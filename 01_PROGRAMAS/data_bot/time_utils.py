from datetime import datetime, timezone, timedelta, time as dt_time
from zoneinfo import ZoneInfo
import config
from config import TIMEZONE_NAME

MADRID_TZ = ZoneInfo(TIMEZONE_NAME)
MARKET_TZ = ZoneInfo("America/New_York")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_madrid() -> datetime:
    return datetime.now(MADRID_TZ)


def ts_utc_str() -> str:
    return now_utc().isoformat()


def ts_madrid_str() -> str:
    return now_madrid().isoformat()


def madrid_date_str() -> str:
    return now_madrid().strftime("%Y-%m-%d")


def current_month_key() -> str:
    return now_madrid().strftime("%Y-%m")


def session_label_from_time(hhmm: str) -> str:
    mapping = {
        "14:25": "premarket",
        "16:00": "market_1",
        "18:30": "market_2",
        "20:45": "market_3",
        "22:05": "postmarket",
        "15:00": "gex_1500",
        "15:30": "gex_1530",
        "16:30": "gex_1630",
        "17:30": "gex_1730",
        "19:30": "gex_1930",
        "20:30": "gex_2030",
        "21:30": "gex_2130",
        "21:50": "gex_2150",
        "22:15": "gex_2215",
    }
    return mapping.get(hhmm, hhmm.replace(":", ""))


def market_session_date(dt: datetime | None = None):
    value = dt or now_madrid()
    if value.tzinfo is None:
        value = value.replace(tzinfo=MADRID_TZ)
    return value.astimezone(MARKET_TZ).date()


def market_session_date_str(dt: datetime | None = None) -> str:
    return market_session_date(dt).isoformat()


def market_date_from_madrid_iso(value: str) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MADRID_TZ)
        return dt.astimezone(MARKET_TZ).date().isoformat()
    except Exception:
        return str(value)[:10]


def latest_completed_market_date(dt: datetime | None = None, close_grace_minutes: int = 10):
    """Return the latest US market weekday whose regular session has completed.

    It intentionally does not invent holiday data. Startup repair additionally
    requires an actual successful Scanner run for the candidate date, so a US
    market holiday cannot be backfilled as a synthetic deep close.
    """
    value = dt or now_madrid()
    if value.tzinfo is None:
        value = value.replace(tzinfo=MADRID_TZ)
    ny = value.astimezone(MARKET_TZ)
    candidate = ny.date()
    close_minutes = 16 * 60 + max(0, int(close_grace_minutes))
    current_minutes = ny.hour * 60 + ny.minute
    if ny.weekday() >= 5 or current_minutes < close_minutes:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _clock_minutes(value: str, *, end: bool = False) -> int:
    text = str(value or '').strip()
    if text in {'24:00', '24'}:
        return 24 * 60
    try:
        hour, minute = [int(x) for x in text.split(':', 1)]
    except Exception:
        return 24 * 60 if end else 0
    if end and hour == 0 and minute == 0:
        return 24 * 60
    return max(0, min(24 * 60, hour * 60 + minute))


def is_operational_window(dt: datetime | None = None) -> bool:
    """Mon-Fri Madrid 12:00-00:00 by default; startup remains independent."""
    if not bool(getattr(config, 'OPERATING_WINDOW_ENABLED', True)):
        return True
    value = dt or now_madrid()
    if value.tzinfo is None:
        value = value.replace(tzinfo=MADRID_TZ)
    else:
        value = value.astimezone(MADRID_TZ)
    weekdays = tuple(getattr(config, 'OPERATING_WEEKDAYS', (0, 1, 2, 3, 4)))
    if value.weekday() not in weekdays:
        return False
    current = value.hour * 60 + value.minute
    start = _clock_minutes(getattr(config, 'OPERATING_WINDOW_START', '12:00'))
    end = _clock_minutes(getattr(config, 'OPERATING_WINDOW_END', '00:00'), end=True)
    if end >= start:
        return start <= current < end
    return current >= start or current < end


def next_operational_start(dt: datetime | None = None):
    value = dt or now_madrid()
    if value.tzinfo is None:
        value = value.replace(tzinfo=MADRID_TZ)
    else:
        value = value.astimezone(MADRID_TZ)
    start = _clock_minutes(getattr(config, 'OPERATING_WINDOW_START', '12:00'))
    hour, minute = divmod(min(start, 23 * 60 + 59), 60)
    candidate = value.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= value:
        candidate += timedelta(days=1)
    weekdays = tuple(getattr(config, 'OPERATING_WEEKDAYS', (0, 1, 2, 3, 4)))
    while candidate.weekday() not in weekdays:
        candidate += timedelta(days=1)
    return candidate
