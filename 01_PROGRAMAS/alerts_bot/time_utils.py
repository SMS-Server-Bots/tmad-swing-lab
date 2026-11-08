from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import config

TZ = ZoneInfo(config.TIMEZONE_NAME)
MARKET_TZ = ZoneInfo(config.ENTRY_MARKET_TIMEZONE)

def now_madrid():
    return datetime.now(TZ)

def ts_utc_str():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def ts_madrid_str():
    return now_madrid().replace(microsecond=0).isoformat()

def today_key():
    return market_session_date_str()


def market_session_date(dt=None):
    value = dt or now_madrid()
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(MARKET_TZ).date()


def market_session_date_str(dt=None):
    return market_session_date(dt).isoformat()


def market_date_from_madrid_iso(value: str) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(MARKET_TZ).date().isoformat()
    except Exception:
        return str(value)[:10]


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


def is_operational_window(dt=None) -> bool:
    """Mon-Fri Madrid 12:00-00:00 by default; no DST maintenance needed."""
    if not bool(getattr(config, 'OPERATING_WINDOW_ENABLED', True)):
        return True
    value = dt or now_madrid()
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    else:
        value = value.astimezone(TZ)
    weekdays = tuple(getattr(config, 'OPERATING_WEEKDAYS', (0, 1, 2, 3, 4)))
    if value.weekday() not in weekdays:
        return False
    current = value.hour * 60 + value.minute
    start = _clock_minutes(getattr(config, 'OPERATING_WINDOW_START', '12:00'))
    end = _clock_minutes(getattr(config, 'OPERATING_WINDOW_END', '00:00'), end=True)
    if end >= start:
        return start <= current < end
    return current >= start or current < end


def next_operational_start(dt=None):
    value = dt or now_madrid()
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    else:
        value = value.astimezone(TZ)
    start = _clock_minutes(getattr(config, 'OPERATING_WINDOW_START', '12:00'))
    hour, minute = divmod(min(start, 23 * 60 + 59), 60)
    weekdays = tuple(getattr(config, 'OPERATING_WEEKDAYS', (0, 1, 2, 3, 4)))
    candidate = value.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= value:
        from datetime import timedelta
        candidate += timedelta(days=1)
    while candidate.weekday() not in weekdays:
        from datetime import timedelta
        candidate += timedelta(days=1)
    return candidate
