from __future__ import annotations

"""Price-only research backfill: never write canonical Scanner/GEX/LIVE DBs.

Source: Yahoo daily chart API, opt-in research evidence only. All OHLC candles
remain segregated from production price_cache; Yahoo is an unofficial source.
Split events, invalid bars, ambiguous symbols and mismatched anchors are
quarantined. Download only train+validation required by frozen selections.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import config
from autonomous_research_cache import cache_filename, load_fast_research_cache, split_cached_rows
from core_context_precision_optimizer import _family_models, _fast_cache_contract_hash, _fit_train_models
from research_joint_entry_exit_stress import _daily, _select, _specs, _entry_keys
from research_swing_exit_policy import _future_path, _load_db_price_paths, _valid_ohlc

RESEARCH_DB = config.DATA_ROOT / "alerts_bot" / "db" / "research_ohlc_backfill.sqlite"
STATUS_FILE = config.OUTPUT_DIR / "historical_validation" / "latest_research_ohlc_backfill.json"
SOURCE = "YAHOO_CHART_DAILY_RESEARCH"
NY = ZoneInfo("America/New_York")
SCHEMA = 1
MAX_WINDOW_CALENDAR_DAYS = 300


def _open_db(path: Path = RESEARCH_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS research_ohlc(
          ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          split_factor REAL NOT NULL DEFAULT 1.0, source TEXT NOT NULL,
          source_symbol TEXT NOT NULL, fetched_at_utc TEXT NOT NULL,
          PRIMARY KEY (ticker, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_research_ohlc_trade_date ON research_ohlc(trade_date,ticker);
        CREATE TABLE IF NOT EXISTS requests(
          ticker TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT,
          started_at_utc TEXT, finished_at_utc TEXT,
          bars INTEGER NOT NULL DEFAULT 0, first_date TEXT, last_date TEXT
        );
    """)
    return conn


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(x: object) -> float | None:
    try:
        z = float(x)
        return z if math.isfinite(z) else None
    except (ValueError, TypeError):
        return None


def _symbol(ticker: str) -> str | None:
    ticker = ticker.upper().strip()
    if not ticker or len(ticker) > 18 or not all(c.isalnum() or c in ".^-" for c in ticker):
        return None
    # Explicit mapping: Yahoo share classes use '-' instead of '.'.
    # Never silently map warrants/crypto, or non-US exchange suffixes.
    return ticker.replace(".", "-") if not ticker.startswith("^") else ticker


def _parse_chart(payload: dict, ticker: str, start: date, end: date) -> tuple[list[dict], str | None]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        return [], "API_ERROR"
    results = chart.get("result") or []
    if not results:
        return [], "EMPTY_CHART"
    result = results[0]
    meta = result.get("meta") or {}
    if str(meta.get("currency") or "").upper() != "USD":
        return [], "NON_USD_INSTRUMENT"
    if str(meta.get("instrumentType") or "").upper() not in {"EQUITY", "ETF"}:
        return [], "NON_EQUITY_OR_ETF"
    resolved = str(meta.get("symbol") or "").upper()
    if resolved != _symbol(ticker):
        return [], "SYMBOL_MISMATCH"
    if (result.get("events") or {}).get("splits"):
        # A split can change historical price bases; do not mix/guess adjustment.
        return [], "SPLIT_EVENT_IN_REQUESTED_WINDOW"
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not isinstance(quote, dict):
        return [], "INVALID_QUOTES"
    columns = [quote.get(field) or [] for field in ("open", "high", "low", "close")]
    if not timestamps or any(len(col) != len(timestamps) for col in columns):
        return [], "INCOMPLETE_OHLC_COLUMNS"
    bars: list[dict] = []
    seen = set()
    for i, timestamp in enumerate(timestamps):
        try:
            local_date = datetime.fromtimestamp(int(timestamp), NY).date()
        except (ValueError, TypeError, OSError, OverflowError):
            return [], "INVALID_TIMESTAMP"
        if local_date < start or local_date > end:
            continue
        values = [_f(col[i]) for col in columns]
        if any(v is None or v <= 0 for v in values):
            return [], "MISSING_OHLC_BAR"
        op, hi, lo, cl = values
        if lo > min(op, cl) or hi < max(op, cl) or lo > hi:
            return [], "BAD_OHLC_GEOMETRY"
        day = local_date.isoformat()
        if day in seen:
            return [], "DUPLICATE_DATE"
        seen.add(day)
        bars.append({"ticker": ticker, "trade_date": day, "open": op, "high": hi,
                     "low": lo, "close": cl, "split_factor": 1.0,
                     "source": SOURCE, "source_symbol": resolved})
    if not bars:
        return [], "NO_ROWS_IN_DATE_WINDOW"
    bars.sort(key=lambda x: x["trade_date"])
    previous = None
    for bar in bars:
        if previous is not None and abs(bar["open"] / previous - 1) > 0.5:
            return [], "UNVERIFIED_DISCONTINUITY"
        previous = bar["close"]
    return bars, None


def _download(ticker: str, start: date, end: date) -> tuple[list[dict], str | None]:
    symbol = _symbol(ticker)
    if not symbol:
        return [], "SYMBOL_NOT_SUPPORTED"
    params = {
        "period1": int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()),
        "interval": "1d", "events": "splits", "includeAdjustedClose": "true",
    }
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="") + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TMADResearchOHLC/1.0)", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=22) as resp:
            if int(resp.status) != 200:
                return [], "HTTP_" + str(resp.status)
            data = resp.read(3_000_000)
        return _parse_chart(json.loads(data), ticker, start, end)
    except HTTPError as exc:
        return [], "HTTP_" + str(exc.code)
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return [], "NETWORK_OR_PAYLOAD_ERROR"


def _required_keys() -> tuple[dict[str, list[str]], dict]:
    unique: set[tuple[str, str]] = set()
    sample: dict = {}
    recorded = _entry_keys()
    specs = _specs()
    configs = {(x["selection"], x["side"], x["capacity"]): x for x in specs}
    for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
        payload = load_fast_research_cache(
            config.RUNTIME_ROOT / "autonomous_research" / "cache" / cache_filename(lane),
            expected_contract_hash=_fast_cache_contract_hash(), verify_fingerprint=True,
        )
        train, validation, _locked_test = split_cached_rows(payload)
        train_days = sorted({str(x.get("date") or "") for x in train})
        validation_days = sorted({str(x.get("date") or "") for x in validation})
        train = [r for r in train if r.get("date") in set(train_days[:-5])]
        validation = [r for r in validation if r.get("date") in set(validation_days[:-5])]
        models = _family_models(_fit_train_models(train), "CONTEXT_LEAN")
        day_train, diag = _daily(train, models)
        day_valid, _ = _daily(validation, models)
        for days in (day_train, day_valid):
            for spec in configs.values():
                for item in _select(days, spec, recorded, high_conf_threshold=diag["high_conf_threshold_from_this_split"]):
                    unique.add((item["ticker"], item["signal_date"]))
        sample[lane] = {"fingerprint": payload.get("dataset_fingerprint"),
                        "train_days": len(train_days)-5, "validation_days": len(validation_days)-5}
    grouped = defaultdict(list)
    for ticker, day in sorted(unique):
        grouped[ticker].append(day)
    return dict(grouped), {"signal_keys": len(unique), "tickers": len(grouped), "cache": sample, "final_test_locked": True}



def _request_windows(signal_dates: list[str], maturity_cutoff: date) -> list[tuple[date, date]]:
    """Cover every signal's 10 future sessions; bounded and isolated windows.

    The old single 300-day download silently discarded late signal dates
    while still marking the ticker OK. Do not download wide intervening gaps
    that could contain an unrelated split and poison valid signal windows.
    """
    windows: list[tuple[date, date]] = []
    for value in sorted(set(signal_dates)):
        day = date.fromisoformat(value)
        start = day + timedelta(days=1)
        end = min(day + timedelta(days=35), maturity_cutoff)
        if end <= start:
            continue
        if windows:
            last_start, last_end = windows[-1]
            if (start - last_end).days <= 12 and (end - last_start).days <= MAX_WINDOW_CALENDAR_DAYS:
                windows[-1] = (last_start, max(last_end, end))
                continue
        windows.append((start, end))
    return windows


def _coverage(signal_dates: list[str], bars: list[dict]) -> tuple[int, Counter]:
    """Only mark ticker fully covered if each required forward path is valid."""
    failures: Counter = Counter()
    covered = 0
    for day in signal_dates:
        _, reason = _future_path({"signal_date": day}, bars, sessions=10)
        if reason is None:
            covered += 1
        else:
            failures[str(reason)] += 1
    return covered, failures

def _overlap_ok(bars: list[dict], baseline: list[dict]) -> tuple[bool, str]:
    ref = {str(x.get("trade_date") or ""): x for x in baseline
           if _valid_ohlc(x, signal_date="1900-01-01") is not None}
    ratios = []
    for bar in bars:
        old = ref.get(bar["trade_date"])
        if old:
            previous = _f(old.get("close"))
            if previous and previous > 0:
                ratios.append(abs(bar["close"] / previous - 1.0))
    # A sourced local bar is stronger evidence than a Yahoo bar. If the
    # price bases materially disagree, quarantine the symbol; never stitch.
    if ratios and max(ratios) > 0.08:
        return False, "EXISTING_TRUSTED_CLOSE_CONFLICT"
    return True, "MATCHED" if ratios else "NO_REFERENCE_OVERLAP"


def _store(conn: sqlite3.Connection, ticker: str, bars: list[dict]) -> None:
    stamp = _utc()
    conn.executemany(
        "INSERT INTO research_ohlc(ticker,trade_date,open,high,low,close,split_factor,"
        "source,source_symbol,fetched_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker,trade_date) DO UPDATE SET open=excluded.open,"
        "high=excluded.high,low=excluded.low,close=excluded.close,"
        "split_factor=excluded.split_factor,source=excluded.source,"
        "source_symbol=excluded.source_symbol,fetched_at_utc=excluded.fetched_at_utc",
        [(ticker, b["trade_date"], b["open"], b["high"], b["low"], b["close"],
          1.0, SOURCE, b["source_symbol"], stamp) for b in bars],
    )


def _record(conn: sqlite3.Connection, ticker: str, status: str, reason: str, bars: list[dict]) -> None:
    conn.execute("INSERT INTO requests(ticker,status,reason,finished_at_utc,bars,first_date,last_date)"
                 " VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
                 "status=excluded.status,reason=excluded.reason,"
                 "finished_at_utc=excluded.finished_at_utc,bars=excluded.bars,"
                 "first_date=excluded.first_date,last_date=excluded.last_date",
                 (ticker, status, reason, _utc(), len(bars),
                  bars[0]["trade_date"] if bars else None,
                  bars[-1]["trade_date"] if bars else None))


def _revision(conn: sqlite3.Connection) -> str:
    # Content-addressed: changing a price without changing row count must
    # invalidate all previous exit-policy results, even within one second.
    digest = sha256()
    for row in conn.execute(
        "SELECT ticker,trade_date,open,high,low,close,split_factor,source "
        "FROM research_ohlc ORDER BY ticker,trade_date"
    ):
        digest.update(json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _write_report(report: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def run(*, max_tickers: int = 400, pause_seconds: float = 1.25,
        fetcher=None, dry_run: bool = False) -> dict:
    needed, diagnosis = _required_keys()
    all_entries = [{"ticker": ticker, "signal_date": day}
                   for ticker, dates in needed.items() for day in dates]
    # The query layer reads existing local observations + previously verified
    # research supplement without mutating canonical Scanner or Alerts data.
    existing, _ = _load_db_price_paths(all_entries)
    gaps = defaultdict(list)
    reasons = Counter()
    maturity_cutoff = datetime.now(NY).date() - timedelta(days=1)
    for (ticker, day), data in existing.items():
        if date.fromisoformat(day) > maturity_cutoff - timedelta(days=22):
            # Avoid attempting impossible 10-session forward research on recent dates.
            reasons["NOT_YET_MATURE_10_SESSIONS"] += 1
            continue
        _path, failure = _future_path({"signal_date": day}, data, sessions=10)
        if failure:
            reasons[failure] += 1
            gaps[ticker].append(day)
    result = {"schema_version": SCHEMA, "mode": "SEPARATE_RESEARCH_OHLC_BACKFILL_V1",
              "generated_at_madrid": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
              "provider": SOURCE, "provider_is_unofficial": True, "live_mutation": False,
              "scanner_gex_mutation": False, "final_test_locked": True,
              "dry_run": dry_run, "required": diagnosis,
              "gaps": {"keys": sum(map(len, gaps.values())), "tickers": len(gaps),
                       "reasons": dict(reasons)},
              "outcomes": {}, "errors": {}, "attempted": 0, "downloaded_tickers": 0,
              "downloaded_bars": 0, "request_cap": max_tickers}
    if dry_run:
        _write_report(result)
        return result
    conn = _open_db()
    try:
        previous = {row[0]: (str(row[1] or ""), str(row[2] or ""))
                    for row in conn.execute(
                        "SELECT ticker,status,finished_at_utc FROM requests"
                    )}
        # Every ticker with an ACTUAL remaining gap is eligible even if an old
        # incomplete download incorrectly recorded OK. Prioritize never-tried
        # tickers over retries; across batches, previously attempted symbols
        # move behind the queue. No implicit permanent blacklist.
        planned = sorted(
            gaps.items(),
            key=lambda item: (
                1 if previous.get(item[0], ("", ""))[1] else 0,
                previous.get(item[0], ("", ""))[1],
                item[0],
            ),
        )
        result["still_to_request"] = len(planned)
        result["prior_ok_but_gapped"] = sum(
            previous.get(ticker, ("", ""))[0] == "OK" for ticker in gaps
        )
        fetch = fetcher or _download
        progress = Counter()
        for ticker, dates in planned[:max_tickers]:
            windows = _request_windows(dates, maturity_cutoff)
            collected: dict[str, dict] = {}
            failures = Counter()
            for start, end in windows:
                rows, error = fetch(ticker, start, end)
                if error:
                    failures[str(error)] += 1
                    if error == "HTTP_429":
                        result["rate_limited"] = True
                        break
                else:
                    local = []
                    for day in dates:
                        local.extend(existing.get((ticker, day), []))
                    permitted, why = _overlap_ok(rows, local)
                    if not permitted:
                        failures[why] += 1
                        break
                    # Same-provider overlap across windows must also agree:
                    # never silently overwrite different price bases.
                    for bar in rows:
                        prior = collected.get(bar["trade_date"])
                        if prior and abs(bar["close"] / prior["close"] - 1.0) > 0.01:
                            failures["PROVIDER_WINDOW_CLOSE_CONFLICT"] += 1
                            break
                        collected[bar["trade_date"]] = bar
                    if "PROVIDER_WINDOW_CLOSE_CONFLICT" in failures:
                        break
                if pause_seconds > 0:
                    time.sleep(pause_seconds)
            merged = sorted(collected.values(), key=lambda x: x["trade_date"])
            # A provider-basis or anchor conflict quarantines ALL new prices
            # from that ticker; transport/transient gaps may keep separately
            # verified same-provider windows for future deterministic resume.
            fatal = bool(
                failures.get("EXISTING_TRUSTED_CLOSE_CONFLICT")
                or failures.get("PROVIDER_WINDOW_CLOSE_CONFLICT")
            )
            prior_verified = {
                str(row[0]): {
                    "ticker": ticker, "trade_date": str(row[0]), "open": row[1],
                    "high": row[2], "low": row[3], "close": row[4],
                    "source": SOURCE, "split_factor": 1.0,
                }
                for row in conn.execute(
                    "SELECT trade_date,open,high,low,close FROM research_ohlc WHERE ticker=?",
                    (ticker,),
                )
            }
            combined = dict(prior_verified)
            if not fatal:
                combined.update({row["trade_date"]: row for row in merged})
            # Verify provider-only paths from all successful previous batches,
            # never count a quarantined network response as coverage.
            verified, incomplete = _coverage(
                dates, sorted(combined.values(), key=lambda x: x["trade_date"])
            )
            if merged and not fatal:
                _store(conn, ticker, merged)
                result["downloaded_tickers"] += 1
                result["downloaded_bars"] += len(merged)
            complete = verified == len(dates) and not failures and not fatal
            status = "OK" if complete else ("PARTIAL" if merged and not fatal else "REJECTED")
            reason = (
                "" if complete else (
                    ",".join(sorted(failures)) if failures else
                    "INCOMPLETE_FORWARD_SESSIONS"
                )
            )
            _record(conn, ticker, status, reason, merged if merged and not fatal else [])
            progress[status] += 1
            result["verified_signal_paths_in_batch"] = (
                int(result.get("verified_signal_paths_in_batch") or 0) + verified
            )
            result["requested_windows"] = int(result.get("requested_windows") or 0) + len(windows)
            for reason_code, count in failures.items():
                result["errors"][reason_code] = int(result["errors"].get(reason_code) or 0) + count
            for reason_code, count in incomplete.items():
                progress["forward_" + reason_code] += count
            conn.commit()
            result["attempted"] += 1
            if result["attempted"] % 25 == 0:
                print("[PRICE_BACKFILL] attempted={} complete={} partial={} rejected={}".format(
                    result["attempted"], progress["OK"], progress["PARTIAL"], progress["REJECTED"]
                ), flush=True)
            if result.get("rate_limited"):
                break
        result["outcomes"]["research_rows_total"] = conn.execute(
            "SELECT COUNT(*) FROM research_ohlc"
        ).fetchone()[0]
        result["outcomes"]["revision"] = _revision(conn)
        result["outcomes"]["newly_unattempted_queue_remaining"] = sum(
            1 for ticker, _ in planned[max_tickers:]
            if not previous.get(ticker, ("", ""))[1]
        )
        result["outcomes"]["batch_unattempted_tickers"] = max(0, len(planned) - result["attempted"])
        result["outcomes"]["batch_statuses"] = dict(progress)
        # Recalculate the actual OHLC coverage after persisted rows are visible,
        # not just whether a network request or a ticker had a successful HTTP.
        after, _diag = _load_db_price_paths(all_entries)
        still_missing = Counter()
        for (ticker, day), data in after.items():
            if date.fromisoformat(day) > maturity_cutoff - timedelta(days=22):
                continue
            _, failure = _future_path({"signal_date": day}, data, sessions=10)
            if failure:
                still_missing[ticker] += 1
        result["outcomes"]["gaps_after_keys"] = sum(still_missing.values())
        result["outcomes"]["gaps_after_tickers"] = len(still_missing)
        result["outcomes"]["pending_tickers"] = len(still_missing)
        result["outcomes"]["verified_gap_reduction"] = max(
            0, result["gaps"]["keys"] - sum(still_missing.values())
        )
        result["outcomes"]["coverage_status"] = (
            "COMPLETE" if not still_missing else "INCOMPLETE"
        )
        _write_report(result)
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-tickers", type=int, default=400)
    parser.add_argument("--pause-seconds", type=float, default=1.25)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_tickers <= 5000 or not 0 <= args.pause_seconds <= 60:
        parser.error("bounds: max-tickers 1..5000, pause 0..60")
    out = run(max_tickers=args.max_tickers, pause_seconds=args.pause_seconds, dry_run=args.dry_run)
    print(json.dumps({k: v for k,v in out.items() if k != "required"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
