import os
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

PROGRAM_DIR = Path(__file__).resolve().parent
_VALIDATION_MODE_RAW = str(os.getenv("TMAD_VALIDATION_MODE", "0") or "0").strip().lower()
VALIDATION_MODE = _VALIDATION_MODE_RAW in {"1", "true", "yes", "on"}
_PREEXISTING_ENV = set(os.environ)
INSTALL_ROOT = Path(os.getenv("TMAD_INSTALL_ROOT", PROGRAM_DIR.parents[1])).resolve()
CONFIG_DIR = Path(os.getenv("TMAD_CONFIG_DIR", INSTALL_ROOT / "02_CONFIG")).resolve()
DATA_ROOT = Path(os.getenv("TMAD_DATA_DIR", INSTALL_ROOT / "03_DATOS")).resolve()
RUNTIME_ROOT = Path(os.getenv("TMAD_RUNTIME_DIR", INSTALL_ROOT / "04_RUNTIME")).resolve()

_CONFIG_FILES = (CONFIG_DIR / "configuracion_canales.env", CONFIG_DIR / "data_bot.env", CONFIG_DIR / "data_bot.secret.env")
if not VALIDATION_MODE:
    for _config_file in _CONFIG_FILES:
        load_dotenv(_config_file, override=False)

def config_value_source(name: str, default: str = "internal_default") -> str:
    if name in _PREEXISTING_ENV:
        return "environment"
    if not VALIDATION_MODE:
        for path in _CONFIG_FILES:
            try:
                if name in dotenv_values(path):
                    return path.name
            except Exception:
                pass
    return default

BOT_VERSION = "6.2.67"
def _path(base: Path, value: str) -> Path:
    p = Path(value)
    return p.resolve() if p.is_absolute() else (base / p).resolve()

def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default

TIMEZONE_NAME = os.getenv("TIMEZONE_NAME", "Europe/Madrid")
SESSION_FILE = _path(CONFIG_DIR, os.getenv("SESSION_FILE", "tmad_session.json"))

DB_DIR = _path(DATA_ROOT / "data_bot", os.getenv("DB_DIR", "db/live"))
ARCHIVE_DIR = _path(DATA_ROOT / "data_bot", os.getenv("ARCHIVE_DIR", "db/archive"))
LOG_DIR = _path(DATA_ROOT / "data_bot", os.getenv("LOG_DIR", "logs"))
OUTPUT_DIR = _path(DATA_ROOT / "data_bot", os.getenv("OUTPUT_DIR", "output"))

for d in (DB_DIR, ARCHIVE_DIR, LOG_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

LOCK_DIR = RUNTIME_ROOT / "locks"
LOCK_DIR.mkdir(parents=True, exist_ok=True)

COORDINATION_DIR = RUNTIME_ROOT / "coordination"
COORDINATION_DIR.mkdir(parents=True, exist_ok=True)
COORDINATION_DB = COORDINATION_DIR / "tmad_coordination.db"
COORDINATION_STATE_FILE = COORDINATION_DIR / "data_state.json"
COORDINATION_POLL_SECONDS = _int("COORDINATION_POLL_SECONDS", 10)
COORDINATION_HEARTBEAT_SECONDS = _int("COORDINATION_HEARTBEAT_SECONDS", 60)
COORDINATION_STALE_SECONDS = _int("COORDINATION_STALE_SECONDS", 600)
COORDINATION_MAX_RETRIES = _int("COORDINATION_MAX_RETRIES", 3)
COORDINATION_RETRY_SECONDS = _int("COORDINATION_RETRY_SECONDS", 30)

SCANNER_DB = DB_DIR / "tmad_scanner_live.db"
GEX_DB = DB_DIR / "tmad_gex_live.db"
RAW_DB = DB_DIR / "tmad_raw_live.db"
META_DB = DB_DIR / "tmad_meta_live.db"

SCANNER_URL = os.getenv("SCANNER_URL", "https://tmad.app/api/scanner")
STOCK_DETAIL_URL_TEMPLATE = os.getenv("STOCK_DETAIL_URL_TEMPLATE", "https://tmad.app/api/stock/{ticker}")
STOCK_GEX_URL_TEMPLATE = os.getenv("STOCK_GEX_URL_TEMPLATE", "https://tmad.app/api/stock/{ticker}/gex")
STOCK_INTRADAY_URL_TEMPLATE = os.getenv("STOCK_INTRADAY_URL_TEMPLATE", "https://tmad.app/api/stock/{ticker}/intraday?days=30")

SCANNER_PARAMS = {
    "sort": os.getenv("SCANNER_SORT", "gex_change"),
    "dir": os.getenv("SCANNER_DIR", "desc"),
    "limit": _int("SCANNER_LIMIT", 10000),
    "offset": 0,
}
HTTP_TIMEOUT_SECONDS = _float("HTTP_TIMEOUT_SECONDS", 10.0)
HTTP_RETRIES = _int("HTTP_RETRIES", 3)
HTTP_SLEEP_SECONDS = _float("HTTP_SLEEP_SECONDS", 1.0)

# Scanner pagination / robustness
SCANNER_PAGE_SIZE = _int("SCANNER_PAGE_SIZE", 500)
SCANNER_MAX_PAGES = _int("SCANNER_MAX_PAGES", 10)
SCANNER_PAGINATION_MODE = os.getenv("SCANNER_PAGINATION_MODE", "auto")
SAVE_SCANNER_ROWS_BEFORE_ENRICH = _bool("SAVE_SCANNER_ROWS_BEFORE_ENRICH", True)
# Exact equality remains the preferred invariant. TMAD's live ranking can move
# between pagination requests, so a tiny declared-total mismatch must not discard
# an otherwise complete intraday run. Near-complete acceptance is explicit,
# bounded and reported; larger gaps remain partial and are never handed to Alerts.
SCANNER_STRICT_TOTAL_VALIDATION = _bool("SCANNER_STRICT_TOTAL_VALIDATION", True)
SCANNER_PAGINATION_PASSES = _int("SCANNER_PAGINATION_PASSES", 3)
SCANNER_NEAR_COMPLETE_ENABLED = _bool("SCANNER_NEAR_COMPLETE_ENABLED", True)
SCANNER_NEAR_COMPLETE_MIN_RATIO = _float("SCANNER_NEAR_COMPLETE_MIN_RATIO", 0.995)
SCANNER_NEAR_COMPLETE_MAX_SHORTFALL = _int("SCANNER_NEAR_COMPLETE_MAX_SHORTFALL", 3)
# A failed post-close FULL DEEP must not silently consume its daily slot.
# Retry only the scheduled deep slot, bounded and backoff-controlled; SELECTIVE behavior is unchanged.
SCANNER_DEEP_SLOT_RETRY_ATTEMPTS = max(1, min(4, _int("SCANNER_DEEP_SLOT_RETRY_ATTEMPTS", 3)))
SCANNER_DEEP_SLOT_RETRY_BACKOFF_SECONDS = max(30.0, min(900.0, _float("SCANNER_DEEP_SLOT_RETRY_BACKOFF_SECONDS", 120.0)))

DETAIL_TOP_N = _int("DETAIL_TOP_N", 10000)

# Two-tier enrichment: the full Scanner is persisted every hour, while expensive
# detail/GEX calls use a bounded priority universe. A daily deep run refreshes all tickers.
HOURLY_SELECTIVE_ENRICHMENT = _bool("HOURLY_SELECTIVE_ENRICHMENT", True)
HOURLY_ENRICH_MAX_TICKERS = _int("HOURLY_ENRICH_MAX_TICKERS", 180)
FIRST_RUN_ENRICH_MAX_TICKERS = _int("FIRST_RUN_ENRICH_MAX_TICKERS", 220)
DEEP_ENRICH_MAX_TICKERS = _int("DEEP_ENRICH_MAX_TICKERS", 10000)
DAILY_DEEP_CAPTURE_TIMES = _list("DAILY_DEEP_CAPTURE_TIMES", "22:05")
DEEP_CAPTURE_ON_FIRST_RUN = _bool("DEEP_CAPTURE_ON_FIRST_RUN", False)
RELATIONAL_CORE_TICKERS = {x.upper() for x in _list(
    "RELATIONAL_CORE_TICKERS",
    "SPY,QQQ,IWM,DIA,SPX,VIX,VVIX,VIXY,VIXM,VXZ,UVIX,UVXY,VXX,SVIX,SVXY,ZIVB,XLK,XLF,XLE,XLV,XLY,XLP,XLI,XLB,XLU,XLRE,XLC,SMH,SOXX,IGV,HACK,CIBR,KBE,KRE,XBI,IBB,OIH,XOP,XRT,XHB,ITB,JETS,ITA,XAR,XME,SLX,GDX,GDXJ,TAN,ARKK,ARKG,ARKF,BOTZ,ROBO,URA,COPX,LIT,REMX,MJ,TLT,HYG,LQD,GLD,SLV,IBIT,ETHA,TQQQ,SQQQ,SPXL,SPXS,TNA,TZA,UDOW,SDOW,SOXL,SOXS,FAS,FAZ,DPST,WDRW,LABU,LABD,ERX,ERY,RETL,EMTY,TARK,SARK,NUGT,DUST,JNUG,JDST"
)}
ALWAYS_ENRICH_TICKERS = {x.upper() for x in _list("ALWAYS_ENRICH_TICKERS", "SPY,QQQ,IWM,DIA,SPX")} | RELATIONAL_CORE_TICKERS
HOURLY_TOP_GEX_CHANGE = _int("HOURLY_TOP_GEX_CHANGE", 90)
HOURLY_TOP_NEAR_WALL = _int("HOURLY_TOP_NEAR_WALL", 50)
HOURLY_TOP_RVOL = _int("HOURLY_TOP_RVOL", 40)
HOURLY_TOP_SIGNALS = _int("HOURLY_TOP_SIGNALS", 60)
HOURLY_CONTINUITY_TICKERS = _int("HOURLY_CONTINUITY_TICKERS", 40)
HOURLY_MIN_GEX_OK_TICKERS = _int("HOURLY_MIN_GEX_OK_TICKERS", 25)
HOURLY_MIN_GEX_OK_RATIO = _float("HOURLY_MIN_GEX_OK_RATIO", 0.35)
RECENT_ALERT_PRIORITY_DAYS = _int("RECENT_ALERT_PRIORITY_DAYS", 3)
ALERTS_DB_PATH = _path(DATA_ROOT / "alerts_bot", os.getenv("ALERTS_DB_PATH", "db/live/tmad_alerts_adaptive.db"))
WRITE_ENRICHMENT_SELECTION_CSV = _bool("WRITE_ENRICHMENT_SELECTION_CSV", True)
# v5.6.6: compare every Scanner row against the previous successful run before
# deciding which expensive detail/GEX calls add information. Full Scanner rows
# remain available for the cross-sectional graph; the compact delta table is the
# authoritative audit of what actually changed between runs.
HOURLY_SCANNER_DIFF_ENABLED = _bool("HOURLY_SCANNER_DIFF_ENABLED", True)
WRITE_SCANNER_DIFF_CSV = _bool("WRITE_SCANNER_DIFF_CSV", True)
FETCH_DETAIL = _bool("FETCH_DETAIL", True)
FETCH_INTRADAY = _bool("FETCH_INTRADAY", True)
FETCH_GEX = _bool("FETCH_GEX", True)
PERSIST_GEX_FROM_SCANNER = _bool("PERSIST_GEX_FROM_SCANNER", True)
SAVE_RAW_PAYLOADS = _bool("SAVE_RAW_PAYLOADS", True)
DISCOVERY_ENABLED = _bool("DISCOVERY_ENABLED", True)

# Additional TMAD modules discovered from UI investigator
FETCH_PRICE_HISTORY = _bool("FETCH_PRICE_HISTORY", True)
STORE_PRICE_HISTORY_PER_RUN = _bool("STORE_PRICE_HISTORY_PER_RUN", False)
STORE_DETAIL_HISTORY_PER_RUN = _bool("STORE_DETAIL_HISTORY_PER_RUN", False)
COMPACT_HISTORY_DAYS = max(_int("COMPACT_HISTORY_DAYS", 800), 730)
# v5.6: TMAD /api/stock/{ticker} already exposes a compact 30-session history.
# This tier is independently calibrated while the richer full-deep and intraday
# histories continue accumulating.
CORE_HISTORY_TARGET_SESSIONS = _int("CORE_HISTORY_TARGET_SESSIONS", 30)
CORE_HISTORY_MIN_TICKERS_PER_DATE = _int("CORE_HISTORY_MIN_TICKERS_PER_DATE", 100)
CORE_HISTORY_MIN_COVERAGE_RATIO = _float("CORE_HISTORY_MIN_COVERAGE_RATIO", 0.50)
# Safety floor for audited current-window exclusions. This has an internal
# default and does not require any change in 02_CONFIG.
CORE_HISTORY_MIN_ELIGIBLE_UNIVERSE_RATIO = _float("CORE_HISTORY_MIN_ELIGIBLE_UNIVERSE_RATIO", 0.95)
CORE_HISTORY_REBUILD_FROM_RAW_ON_START = _bool("CORE_HISTORY_REBUILD_FROM_RAW_ON_START", True)

# v5.6.6: independent, resumable CORE bootstrap. This does not wait for an
# hourly/deep run and does not depend on FETCH_DETAIL, because CORE calibration
# requires /api/stock/{ticker} history + priceHistory even when regular detail
# enrichment has been disabled in an older 02_CONFIG.
CORE_BOOTSTRAP_FETCH_ON_START = _bool("CORE_BOOTSTRAP_FETCH_ON_START", True)
CORE_BOOTSTRAP_STARTUP_MAX_TICKERS = max(1, _int("CORE_BOOTSTRAP_STARTUP_MAX_TICKERS", 180))
CORE_BOOTSTRAP_DAEMON_BATCH_SIZE = max(1, _int("CORE_BOOTSTRAP_DAEMON_BATCH_SIZE", 60))
CORE_BOOTSTRAP_DAEMON_INTERVAL_MINUTES = max(1, _int("CORE_BOOTSTRAP_DAEMON_INTERVAL_MINUTES", 15))
CORE_BOOTSTRAP_REQUEST_PAUSE_SECONDS = max(0.0, _float("CORE_BOOTSTRAP_REQUEST_PAUSE_SECONDS", 0.0))
CORE_BOOTSTRAP_CONTINUE_IN_DAEMON = _bool("CORE_BOOTSTRAP_CONTINUE_IN_DAEMON", True)
# v6.1.25: a single near-complete ticker with a transient detail failure must not
# block the whole universe. It is quarantined only for the current universe/window
# and will be retried automatically when that fingerprint changes.
CORE_BOOTSTRAP_ISOLATED_QUARANTINE_MAX_PENDING = max(0, _int("CORE_BOOTSTRAP_ISOLATED_QUARANTINE_MAX_PENDING", 1))
CORE_BOOTSTRAP_ISOLATED_QUARANTINE_MAX_GAP = max(0, _int("CORE_BOOTSTRAP_ISOLATED_QUARANTINE_MAX_GAP", 2))
# Deep GEX can see short upstream 5xx/timeout bursts. Retry only technical failures;
# 404/unavailable remains a valid non-retryable absence.
GEX_DEEP_TECHNICAL_RETRY_ATTEMPTS = max(1, min(3, _int("GEX_DEEP_TECHNICAL_RETRY_ATTEMPTS", 2)))
GEX_DEEP_TECHNICAL_RETRY_BACKOFF_SECONDS = max(0.0, _float("GEX_DEEP_TECHNICAL_RETRY_BACKOFF_SECONDS", 1.0))
# v6.1.39: conservative parallelism for the 180-name intraday SELECTIVE layer.
# Only network fetches run concurrently; every SQLite/raw/profile write stays
# on the main thread and preserves the original deterministic ticker order.
GEX_SELECTIVE_PARALLEL_FETCH_ENABLED = _bool("GEX_SELECTIVE_PARALLEL_FETCH_ENABLED", True)
GEX_SELECTIVE_FETCH_WORKERS = max(1, min(4, _int("GEX_SELECTIVE_FETCH_WORKERS", 3)))
GEX_SELECTIVE_TECHNICAL_RETRY_ATTEMPTS = max(1, min(3, _int("GEX_SELECTIVE_TECHNICAL_RETRY_ATTEMPTS", 2)))
GEX_SELECTIVE_TECHNICAL_RETRY_BACKOFF_SECONDS = max(0.0, _float("GEX_SELECTIVE_TECHNICAL_RETRY_BACKOFF_SECONDS", 1.0))
CORE_BOOTSTRAP_STATE_FILE = OUTPUT_DIR / "core_bootstrap_state.json"
FETCH_SCORE = _bool("FETCH_SCORE", True)
FETCH_WATCHLIST = _bool("FETCH_WATCHLIST", True)

WATCHLISTS_URL = os.getenv("WATCHLISTS_URL", "https://tmad.app/api/watchlists")
WATCHLIST_DASHBOARD_URL_TEMPLATE = os.getenv("WATCHLIST_DASHBOARD_URL_TEMPLATE", "https://tmad.app/api/dashboard?watchlist={watchlist_id}")
SPARKLINES_URL_TEMPLATE = os.getenv("SPARKLINES_URL_TEMPLATE", "https://tmad.app/api/sparklines?symbols={symbols}")
SCORE_TOP_LONGS_URL = os.getenv("SCORE_TOP_LONGS_URL", "https://tmad.app/api/score/top-longs")
SCORE_BACKTEST_URL = os.getenv("SCORE_BACKTEST_URL", "https://tmad.app/api/score/backtest")
SCORE_BASKET_HISTORY_URL = os.getenv("SCORE_BASKET_HISTORY_URL", "https://tmad.app/api/score/basket-history")
WATCHLIST_MAX_DASHBOARDS = _int("WATCHLIST_MAX_DASHBOARDS", 50)

# Canonical daily closes are the primary rolling calibration memory. Intraday
# runs are archived to daily ZIPs and removed from live DBs after the close.
DAILY_CLOSE_WINDOW_SESSIONS = _int("DAILY_CLOSE_WINDOW_SESSIONS", 30)
DAILY_CLOSE_RETENTION_CALENDAR_DAYS = max(_int("DAILY_CLOSE_RETENTION_CALENDAR_DAYS", 800), 730)
# Detailed live DBs remain compact because verified weekly/intraday/GEX ZIPs are the
# long-term source. Compact daily CORE tables use their own longer retention.
LIVE_RETENTION_DAYS = max(2, _int("LIVE_RETENTION_DAYS", 10))
INTRADAY_ARCHIVE_DIR = _path(ARCHIVE_DIR, os.getenv("INTRADAY_ARCHIVE_DIR", "intraday"))
INTRADAY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
INTRADAY_ARCHIVE_ENABLED = _bool("INTRADAY_ARCHIVE_ENABLED", True)
INTRADAY_ARCHIVE_AFTER_DEEP = _bool("INTRADAY_ARCHIVE_AFTER_DEEP", True)
INTRADAY_DELETE_AFTER_ZIP = _bool("INTRADAY_DELETE_AFTER_ZIP", True)
ARCHIVE_ENABLED = _bool("ARCHIVE_ENABLED", True)
ARCHIVE_YEARS_TO_KEEP = max(0, _int("ARCHIVE_YEARS_TO_KEEP", 0))
RAW_COMPRESS = _bool("RAW_COMPRESS", True)
EXPORT_MONTHLY_CSV = _bool("EXPORT_MONTHLY_CSV", False)
ARCHIVE_MODE = os.getenv("ARCHIVE_MODE", "weekly").strip().lower()
EXPORT_WEEKLY_CSV = _bool("EXPORT_WEEKLY_CSV", True)
EXPORT_WEEKLY_ZIP = _bool("EXPORT_WEEKLY_ZIP", True)
ARCHIVE_PREVIOUS_WEEK = _bool("ARCHIVE_PREVIOUS_WEEK", True)
DELETE_FINALIZED_WEEK_FOLDER_AFTER_ZIP = _bool("DELETE_FINALIZED_WEEK_FOLDER_AFTER_ZIP", True)

# Rolling historical coverage. Before live retention deletes old rows, the Data Bot
# generates/finalizes every weekly archive that can be reconstructed from real local data.
HISTORY_AUTO_GENERATE_ARCHIVES = _bool("HISTORY_AUTO_GENERATE_ARCHIVES", True)
HISTORY_TARGET_DAYS = _int("HISTORY_TARGET_DAYS", 30)
HISTORY_MIN_TRADING_DAYS = _int("HISTORY_MIN_TRADING_DAYS", 18)
HISTORY_REBUILD_INCOMPLETE_ZIPS = _bool("HISTORY_REBUILD_INCOMPLETE_ZIPS", True)
HISTORY_COVERAGE_FILE = OUTPUT_DIR / "history_coverage.json"
HISTORY_COVERAGE_CACHE_FILE = OUTPUT_DIR / "archive_coverage_cache.json"

MIN_VALID_RUN_SNAPSHOTS = _int("MIN_VALID_RUN_SNAPSHOTS", 400)
MIN_VALID_RUN_SNAPSHOT_RATIO = _float("MIN_VALID_RUN_SNAPSHOT_RATIO", 0.80)

LOOP_SLEEP_SECONDS = _int("LOOP_SLEEP_SECONDS", 20)
RUN_WEEKENDS = _bool("RUN_WEEKENDS", False)

# Broad Madrid operating window. Startup recovery always runs once, including
# nights/weekends; after that, outside Mon-Fri 12:00-00:00 only heartbeats and
# coordination liveness are maintained. 00:00 is interpreted as end-of-day.
OPERATING_WINDOW_ENABLED = _bool("OPERATING_WINDOW_ENABLED", True)
OPERATING_WINDOW_START = os.getenv("OPERATING_WINDOW_START", "12:00")
OPERATING_WINDOW_END = os.getenv("OPERATING_WINDOW_END", "00:00")
OPERATING_WEEKDAYS = tuple(int(x) for x in _list("OPERATING_WEEKDAYS", "0,1,2,3,4") if str(x).isdigit())
STARTUP_RECOVERY_ALWAYS = _bool("STARTUP_RECOVERY_ALWAYS", True)
REST_MODE_HEARTBEAT_ONLY = _bool("REST_MODE_HEARTBEAT_ONLY", True)
STARTUP_RECOVERY_MAX_WAIT_SECONDS = _int("STARTUP_RECOVERY_MAX_WAIT_SECONDS", 180)
SCANNER_CAPTURE_TIMES = _list("SCANNER_CAPTURE_TIMES", "14:25,16:00,18:30,20:45,22:05")
GEX_CAPTURE_TIMES = _list("GEX_CAPTURE_TIMES", "15:00,15:30,16:30,17:30,18:30,19:30,20:30,21:30,21:50,22:15")
HEARTBEAT_MINUTES = _int("HEARTBEAT_MINUTES", 60)

DISCORD_WEBHOOK_URL_DATA_LOG = _first_env(
    "DISCORD_WEBHOOK_URL_DATA_LOG", "DATA_LOG_WEBHOOK", "DATA_BOT_LOG_WEBHOOK", "DISCORD_DATA_LOG_WEBHOOK", "WEBHOOK_DATA_LOG", "DISCORD_WEBHOOK_URL_LOG"
)
DISCORD_WEBHOOK_URL_DATA_ERROR = _first_env(
    "DISCORD_WEBHOOK_URL_DATA_ERROR", "DATA_ERROR_WEBHOOK", "DATA_BOT_ERROR_WEBHOOK", "DISCORD_DATA_ERROR_WEBHOOK", "WEBHOOK_DATA_ERROR", "DISCORD_WEBHOOK_URL_ERROR"
)
DISCORD_WEBHOOK_URL_DATA_QUALITY = _first_env(
    "DISCORD_WEBHOOK_URL_DATA_QUALITY", "DATA_QUALITY_WEBHOOK", "DATA_BOT_QUALITY_WEBHOOK", "WEBHOOK_DATA_QUALITY",
    "DISCORD_WEBHOOK_URL_DATA_SCHEMA", "DATA_SCHEMA_WEBHOOK", "DATA_BOT_SCHEMA_WEBHOOK", "DISCORD_DATA_SCHEMA_WEBHOOK", "WEBHOOK_DATA_SCHEMA", "DISCORD_WEBHOOK_URL_SCHEMA"
)
DISCORD_WEBHOOK_URL_DATA_SCHEMA = DISCORD_WEBHOOK_URL_DATA_QUALITY
# Backward-compatible aliases.
DISCORD_WEBHOOK_URL_LOG = DISCORD_WEBHOOK_URL_DATA_LOG
DISCORD_WEBHOOK_URL_ERROR = DISCORD_WEBHOOK_URL_DATA_ERROR
DISCORD_WEBHOOK_URL_SCHEMA = DISCORD_WEBHOOK_URL_DATA_QUALITY
MAX_DISCORD_MESSAGE_CHARS = _int("MAX_DISCORD_MESSAGE_CHARS", 1900)
SEND_SCHEMA_CHANGE_ALERTS = _bool("SEND_SCHEMA_CHANGE_ALERTS", True)
DISCOVERY_BASELINE_SUMMARY_ONLY = _bool("DISCOVERY_BASELINE_SUMMARY_ONLY", True)
DISCOVERY_MAX_FIELDS_IN_ALERT = _int("DISCOVERY_MAX_FIELDS_IN_ALERT", 25)
# Evita spam por cambios normales int<->float o null<->float/string por ticker.
# Déjalo false: el schema channel debe avisar de campos nuevos reales, no de tipos dinámicos.
DISCOVERY_ALERT_TYPE_CHANGES = _bool("DISCOVERY_ALERT_TYPE_CHANGES", False)
DISCOVERY_RECORD_TYPE_CHANGES = _bool("DISCOVERY_RECORD_TYPE_CHANGES", False)
# Si está true, no envía un mensaje por ticker: acumula cambios y manda un resumen al final de cada captura.
DISCOVERY_AGGREGATE_ALERTS = _bool("DISCOVERY_AGGREGATE_ALERTS", True)
# El resumen de schema debe representar el contrato común del endpoint, no rarezas de un ticker concreto.
# Por defecto solo avisa de campos nuevos que aparecen en >=80% de payloads inspeccionados
# (scanner se trata como payload único común).
DISCOVERY_SUPPRESS_RARE_FIELDS = _bool("DISCOVERY_SUPPRESS_RARE_FIELDS", True)
DISCOVERY_COMMON_FIELD_MIN_RATIO = _float("DISCOVERY_COMMON_FIELD_MIN_RATIO", 0.80)
DISCOVERY_COMMON_FIELD_MIN_PAYLOADS = _int("DISCOVERY_COMMON_FIELD_MIN_PAYLOADS", 25)
SEND_RUN_SUMMARY = _bool("SEND_RUN_SUMMARY", True)
# Startup bootstrap / healthcheck
RUN_BOOTSTRAP_ON_START = _bool("RUN_BOOTSTRAP_ON_START", True)
BOOTSTRAP_ARCHIVE_ON_START = _bool("BOOTSTRAP_ARCHIVE_ON_START", True)
BOOTSTRAP_RETENTION_ON_START = _bool("BOOTSTRAP_RETENTION_ON_START", True)
BOOTSTRAP_CAPTURE_IF_EMPTY = _bool("BOOTSTRAP_CAPTURE_IF_EMPTY", True)
BOOTSTRAP_CAPTURE_SCANNER_IF_NO_SUCCESS_TODAY = _bool("BOOTSTRAP_CAPTURE_SCANNER_IF_NO_SUCCESS_TODAY", False)
BOOTSTRAP_CAPTURE_GEX_IF_NO_SUCCESS_TODAY = _bool("BOOTSTRAP_CAPTURE_GEX_IF_NO_SUCCESS_TODAY", False)
BOOTSTRAP_RUN_GEX_IF_EMPTY = _bool("BOOTSTRAP_RUN_GEX_IF_EMPTY", True)
BOOTSTRAP_MIN_SCANNER_OK = _int("BOOTSTRAP_MIN_SCANNER_OK", MIN_VALID_RUN_SNAPSHOTS)
BOOTSTRAP_NOTIFY_DISCORD = _bool("BOOTSTRAP_NOTIFY_DISCORD", True)

# Bootstrap data integrity / repair
BOOTSTRAP_REPAIR_RUN_METADATA_ON_START = _bool("BOOTSTRAP_REPAIR_RUN_METADATA_ON_START", True)
BOOTSTRAP_CAPTURE_SCANNER_IF_INCOMPLETE = _bool("BOOTSTRAP_CAPTURE_SCANNER_IF_INCOMPLETE", True)
BOOTSTRAP_CAPTURE_GEX_IF_INCOMPLETE = _bool("BOOTSTRAP_CAPTURE_GEX_IF_INCOMPLETE", True)
BOOTSTRAP_REQUIRE_SUCCESS_TODAY = _bool("BOOTSTRAP_REQUIRE_SUCCESS_TODAY", True)
BOOTSTRAP_MIN_GEX_OK_RATIO = _float("BOOTSTRAP_MIN_GEX_OK_RATIO", 0.80)

# Operational logging / outputs
FILE_LOGGING_ENABLED = _bool("FILE_LOGGING_ENABLED", True)
WRITE_OUTPUT_SUMMARIES = _bool("WRITE_OUTPUT_SUMMARIES", True)
GEX_LOG_404_EACH = _bool("GEX_LOG_404_EACH", False)
GEX_FAILURE_SAMPLE_LIMIT = _int("GEX_FAILURE_SAMPLE_LIMIT", 25)
GEX_404_AS_ERROR = _bool("GEX_404_AS_ERROR", False)

# GEX by expiry / weekly research
GEX_EXPIRY_ENABLED = _bool("GEX_EXPIRY_ENABLED", True)
GEX_EXPIRY_MAX_DTE = _int("GEX_EXPIRY_MAX_DTE", 31)
GEX_EXPIRY_FRONT_MAX_DTE = _int("GEX_EXPIRY_FRONT_MAX_DTE", 2)
GEX_EXPIRY_SHORT_MAX_DTE = _int("GEX_EXPIRY_SHORT_MAX_DTE", 7)
GEX_EXPIRY_NEXT_MAX_DTE = _int("GEX_EXPIRY_NEXT_MAX_DTE", 14)
GEX_EXPIRY_INCLUDE_UNKNOWN_DTE = _bool("GEX_EXPIRY_INCLUDE_UNKNOWN_DTE", False)
GEX_MIN_CHANGE_DENOMINATOR = _float("GEX_MIN_CHANGE_DENOMINATOR", 1.0)
NORMALIZE_SCANNER_WALL_FIELDS = _bool("NORMALIZE_SCANNER_WALL_FIELDS", True)

# v5.7: reconstructed GEX profiles. One full baseline per ticker/day plus
# compressed row-level deltas for later captures. Events are zipped daily and
# removed from live only after the archive has been verified.
GEX_PROFILE_HISTORY_ENABLED = _bool("GEX_PROFILE_HISTORY_ENABLED", True)
GEX_PROFILE_ARCHIVE_ENABLED = _bool("GEX_PROFILE_ARCHIVE_ENABLED", True)
GEX_PROFILE_ARCHIVE_AFTER_DEEP = _bool("GEX_PROFILE_ARCHIVE_AFTER_DEEP", True)
GEX_PROFILE_DELETE_AFTER_ZIP = _bool("GEX_PROFILE_DELETE_AFTER_ZIP", True)
GEX_PROFILE_ARCHIVE_DIR = _path(ARCHIVE_DIR, os.getenv("GEX_PROFILE_ARCHIVE_DIR", "gex_profiles"))
GEX_PROFILE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
GEX_PROFILE_FLOAT_DECIMALS = _int("GEX_PROFILE_FLOAT_DECIMALS", 6)
GEX_PROFILE_ZLIB_LEVEL = min(9, max(1, _int("GEX_PROFILE_ZLIB_LEVEL", 6)))
GEX_PROFILE_MATERIAL_GEX_PCT = _float("GEX_PROFILE_MATERIAL_GEX_PCT", 5.0)
GEX_PROFILE_MATERIAL_LEVEL_MOVE_PCT = _float("GEX_PROFILE_MATERIAL_LEVEL_MOVE_PCT", 0.25)
GEX_PROFILE_ARCHIVE_YEARS_TO_KEEP = _int("GEX_PROFILE_ARCHIVE_YEARS_TO_KEEP", ARCHIVE_YEARS_TO_KEEP)

# Bootstrap weekend behaviour:
# If false and RUN_WEEKENDS=false, startup bootstrap will healthcheck/archive/retention,
# but will not force scanner/GEX captures on Saturday/Sunday.
BOOTSTRAP_CAPTURE_ON_WEEKENDS = _bool("BOOTSTRAP_CAPTURE_ON_WEEKENDS", False)

# Invalid/partial run cleanup. Keeps DBs final/consumption layers clean: partial/interrupted/failed
# runs are removed only when a valid success run exists for the same snapshot_date.
CLEAN_INVALID_RUNS_AFTER_SUCCESS = _bool("CLEAN_INVALID_RUNS_AFTER_SUCCESS", True)
CLEAN_INVALID_RAW_PAYLOADS = _bool("CLEAN_INVALID_RAW_PAYLOADS", True)
CLEAN_INVALID_EXTRA_FIELDS = _bool("CLEAN_INVALID_EXTRA_FIELDS", True)
CLEAN_INVALID_ARCHIVE_ROWS = _bool("CLEAN_INVALID_ARCHIVE_ROWS", True)
VACUUM_AFTER_INVALID_CLEANUP = _bool("VACUUM_AFTER_INVALID_CLEANUP", False)




# Lock handling
LOCK_AUTO_REMOVE_STALE = _bool("LOCK_AUTO_REMOVE_STALE", True)
LOCK_STALE_MINUTES = _int("LOCK_STALE_MINUTES", 360)
LOCK_REMOVE_BY_AGE = _bool("LOCK_REMOVE_BY_AGE", False)

PIPELINE_DIAGNOSTICS_FILE = OUTPUT_DIR / "pipeline_diagnostics.jsonl"
PIPELINE_STATUS_FILE = OUTPUT_DIR / "pipeline_status.json"


# v6.1.20: validation is hermetic. Never load or use real Discord endpoints.
if VALIDATION_MODE:
    DISCORD_WEBHOOK_URL_DATA_LOG = ""
    DISCORD_WEBHOOK_URL_DATA_ERROR = ""
    DISCORD_WEBHOOK_URL_DATA_QUALITY = ""
    DISCORD_WEBHOOK_URL_DATA_SCHEMA = ""
    DISCORD_WEBHOOK_URL_LOG = ""
    DISCORD_WEBHOOK_URL_ERROR = ""
    DISCORD_WEBHOOK_URL_SCHEMA = ""
    BOOTSTRAP_NOTIFY_DISCORD = False

EFFECTIVE_CONFIG_SUMMARY = {
    "BOT_VERSION": BOT_VERSION,
    "VALIDATION_MODE": VALIDATION_MODE,
}
