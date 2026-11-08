import json
import time
import httpx

from config import (
    SESSION_FILE,
    SCANNER_URL,
    SCANNER_PARAMS,
    STOCK_DETAIL_URL_TEMPLATE,
    STOCK_GEX_URL_TEMPLATE,
    STOCK_INTRADAY_URL_TEMPLATE,
    WATCHLISTS_URL,
    WATCHLIST_DASHBOARD_URL_TEMPLATE,
    SPARKLINES_URL_TEMPLATE,
    SCORE_TOP_LONGS_URL,
    SCORE_BACKTEST_URL,
    SCORE_BASKET_HISTORY_URL,
    HTTP_TIMEOUT_SECONDS,
    HTTP_RETRIES,
    HTTP_SLEEP_SECONDS,
    SCANNER_PAGE_SIZE,
    SCANNER_MAX_PAGES,
    SCANNER_PAGINATION_MODE,
    SCANNER_PAGINATION_PASSES,
)


def load_cookies_from_storage_state() -> dict:
    if not SESSION_FILE.exists():
        raise FileNotFoundError(f"No existe SESSION_FILE: {SESSION_FILE}")
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    cookies = {}
    for c in data.get("cookies", []):
        domain = c.get("domain", "")
        if "tmad.app" in domain and c.get("name"):
            cookies[c["name"]] = c.get("value", "")
    if not cookies:
        raise RuntimeError("tmad_session.json no contiene cookies de tmad.app")
    return cookies


def make_client() -> httpx.Client:
    cookies = load_cookies_from_storage_state()
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "referer": "https://tmad.app/",
        "origin": "https://tmad.app",
    }
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True, headers=headers, cookies=cookies)


def fetch_json_with_retries(client: httpx.Client, url: str, params: dict | None = None, retries: int | None = None) -> tuple[dict, int]:
    max_retries = retries or HTTP_RETRIES
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.get(url, params=params)
            status = resp.status_code
            resp.raise_for_status()
            return resp.json(), status
        except httpx.HTTPStatusError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else None
            if status in (429, 500, 502, 503, 504):
                time.sleep(HTTP_SLEEP_SECONDS * attempt)
                continue
            raise
        except Exception as e:
            last_error = e
            time.sleep(HTTP_SLEEP_SECONDS * attempt)
    raise last_error


def _rows_from_payload(payload: dict) -> list[dict]:
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("items") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return rows if isinstance(rows, list) else []


def _ticker_from_row(row: dict) -> str | None:
    if not isinstance(row, dict):
        return None
    for k in ("ticker", "symbol", "underlyingSymbol", "underlying", "name"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return None


def _merge_unique_rows(existing: list[dict], new_rows: list[dict], seen: set[str]) -> int:
    added = 0
    for r in new_rows:
        t = _ticker_from_row(r)
        # Si no hay ticker, usa el JSON entero como clave aproximada para no perder filas raras.
        key = t or json.dumps(r, sort_keys=True, ensure_ascii=False)[:300]
        if key in seen:
            continue
        seen.add(key)
        existing.append(r)
        added += 1
    return added


def _total_count_from_payload(payload: dict):
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return None
    for k in ("totalCount", "total", "count", "recordsTotal", "filteredCount"):
        v = data.get(k)
        try:
            if v is not None:
                return int(v)
        except Exception:
            pass
    return None


def _candidate_param_sets(page: int, size: int, mode: str) -> list[tuple[str, dict]]:
    base = {k: v for k, v in SCANNER_PARAMS.items() if k not in ("offset", "skip", "start", "page", "pageIndex", "pageSize", "perPage", "take", "limit")}
    strategies = []
    if mode in ("auto", "offset"):
        strategies.append(("offset_limit", {**base, "limit": size, "offset": page * size}))
    if mode in ("auto", "skip"):
        strategies.append(("skip_take", {**base, "take": size, "skip": page * size}))
        strategies.append(("skip_limit", {**base, "limit": size, "skip": page * size}))
    if mode in ("auto", "start"):
        strategies.append(("start_limit", {**base, "limit": size, "start": page * size}))
    if mode in ("auto", "page"):
        strategies.append(("page_pageSize_1based", {**base, "page": page + 1, "pageSize": size}))
        strategies.append(("page_limit_1based", {**base, "page": page + 1, "limit": size}))
        strategies.append(("page_perPage_1based", {**base, "page": page + 1, "perPage": size}))
        strategies.append(("pageIndex_pageSize_0based", {**base, "pageIndex": page, "pageSize": size}))
    return strategies


def fetch_scanner(client: httpx.Client) -> tuple[dict, int]:
    """Fetch the complete Scanner with strict, repeatable pagination.

    TMAD's ranking can move between page requests. A single offset pass may
    therefore contain duplicates and miss symbols even though ``totalCount`` is
    larger. We try a large single request, every supported pagination convention,
    and several passes. A run is marked complete only when unique rows reach the
    largest API total observed.
    """
    size = int(SCANNER_PAGE_SIZE or SCANNER_PARAMS.get("limit", 500) or 500)
    max_pages = int(SCANNER_MAX_PAGES or 10)
    mode = (SCANNER_PAGINATION_MODE or "auto").strip().lower()
    passes = max(1, int(SCANNER_PAGINATION_PASSES))

    final_status = 200
    debug: list[dict] = []
    best_rows: list[dict] = []
    best_payload: dict | None = None
    aggregate_rows: list[dict] = []
    aggregate_seen: set[str] = set()
    api_total = 0
    complete_rows: list[dict] | None = None
    complete_payload: dict | None = None

    base = {k: v for k, v in SCANNER_PARAMS.items()
            if k not in ("offset", "skip", "start", "page", "pageIndex",
                         "pageSize", "perPage", "take", "limit")}

    # First try one stable, large request. Some TMAD deployments honor this and
    # avoid all cross-page movement.
    large_limit = max(int(SCANNER_PARAMS.get("limit", 10000) or 10000), size)
    payload, status = fetch_json_with_retries(
        client, SCANNER_URL, params={**base, "limit": large_limit, "offset": 0}
    )
    final_status = status
    rows = _rows_from_payload(payload)
    total = _total_count_from_payload(payload)
    if total is not None:
        api_total = max(api_total, int(total))
    one_rows: list[dict] = []
    one_seen: set[str] = set()
    added = _merge_unique_rows(one_rows, rows, one_seen)
    debug.append({
        "strategy": "single_large_limit", "pass": 0, "page": 0,
        "params": {**base, "limit": large_limit, "offset": 0},
        "status": status, "rows": len(rows), "added_unique": added,
        "unique_total": len(one_rows), "api_total_count": total,
    })
    if len(one_rows) > len(best_rows):
        best_rows, best_payload = list(one_rows), payload
    _merge_unique_rows(aggregate_rows, one_rows, aggregate_seen)
    if api_total and len(one_rows) == api_total:
        complete_rows, complete_payload = one_rows, payload

    strategy_names = []
    for name, _params in _candidate_param_sets(0, size, mode):
        if name not in strategy_names:
            strategy_names.append(name)

    for strategy_name in strategy_names:
        if complete_rows is not None:
            break
        strategy_union: list[dict] = []
        strategy_seen: set[str] = set()
        for pass_no in range(passes):
            rows_acc: list[dict] = []
            seen: set[str] = set()
            base_payload = None
            pass_total = None
            for page in range(max_pages):
                candidates = dict(_candidate_param_sets(page, size, mode))
                params = candidates.get(strategy_name)
                if params is None:
                    break
                page_payload, status = fetch_json_with_retries(client, SCANNER_URL, params=params)
                final_status = status
                if base_payload is None:
                    base_payload = page_payload
                page_rows = _rows_from_payload(page_payload)
                total = _total_count_from_payload(page_payload)
                if total is not None:
                    pass_total = int(total)
                    api_total = max(api_total, pass_total)
                added = _merge_unique_rows(rows_acc, page_rows, seen)
                debug.append({
                    "strategy": strategy_name, "pass": pass_no, "page": page,
                    "params": params, "status": status, "rows": len(page_rows),
                    "added_unique": added, "unique_total": len(rows_acc),
                    "api_total_count": total,
                })
                if not page_rows or added == 0:
                    break
                if pass_total and len(rows_acc) >= pass_total:
                    break

            if len(rows_acc) > len(best_rows):
                best_rows, best_payload = list(rows_acc), base_payload
            _merge_unique_rows(strategy_union, rows_acc, strategy_seen)
            _merge_unique_rows(aggregate_rows, rows_acc, aggregate_seen)

            expected = api_total or pass_total or 0
            if expected and len(rows_acc) == expected:
                complete_rows, complete_payload = rows_acc, base_payload
                break
            if expected and len(strategy_union) == expected:
                complete_rows, complete_payload = strategy_union, base_payload
                break
            if expected and len(aggregate_rows) == expected:
                complete_rows, complete_payload = aggregate_rows, base_payload
                break

    # v6.1.43: TMAD Scanner is a moving ranking. If normal pagination
    # leaves a material hole, walk the same ranking in reverse and merge unique
    # symbols. Strict total/near-complete acceptance remains unchanged.
    if complete_rows is None and api_total and len(aggregate_rows) < api_total:
        normal_dir = str(SCANNER_PARAMS.get("dir") or "desc").strip().lower()
        reverse_dir = "asc" if normal_dir == "desc" else "desc"
        recovery_base = {**base, "dir": reverse_dir}
        for recovery_pass in range(2):
            for page in range(max_pages):
                params = {**recovery_base, "limit": size, "offset": page * size}
                page_payload, status = fetch_json_with_retries(client, SCANNER_URL, params=params)
                final_status = status
                page_rows = _rows_from_payload(page_payload)
                total = _total_count_from_payload(page_payload)
                if total is not None:
                    api_total = max(api_total, int(total))
                added = _merge_unique_rows(aggregate_rows, page_rows, aggregate_seen)
                debug.append({
                    "strategy": "reverse_offset_recovery", "pass": recovery_pass,
                    "page": page, "params": params, "status": status,
                    "rows": len(page_rows), "added_unique": added,
                    "unique_total": len(aggregate_rows), "api_total_count": total,
                })
                if len(aggregate_rows) == api_total:
                    complete_rows, complete_payload = list(aggregate_rows), page_payload
                    break
                if not page_rows or (added == 0 and page > 0):
                    break
            if complete_rows is not None:
                break

    # v6.1.84: when a moving Scanner still leaves a small pagination hole,
    # retry the single large snapshot and union unique symbols. This preserves the
    # strict completeness contract while giving the API a chance to settle between
    # snapshots; no ranking/selection thresholds are relaxed.
    if complete_rows is None and api_total and len(aggregate_rows) < api_total:
        for recovery_pass in range(3):
            if recovery_pass:
                time.sleep(min(1.0, max(0.1, float(HTTP_SLEEP_SECONDS))))
            recovery_payload, status = fetch_json_with_retries(
                client, SCANNER_URL, params={**base, "limit": large_limit, "offset": 0}
            )
            final_status = status
            recovery_rows = _rows_from_payload(recovery_payload)
            total = _total_count_from_payload(recovery_payload)
            if total is not None:
                api_total = max(api_total, int(total))
            added = _merge_unique_rows(aggregate_rows, recovery_rows, aggregate_seen)
            debug.append({
                "strategy": "large_snapshot_recovery", "pass": recovery_pass,
                "page": 0, "params": {**base, "limit": large_limit, "offset": 0},
                "status": status, "rows": len(recovery_rows),
                "added_unique": added, "unique_total": len(aggregate_rows),
                "api_total_count": total,
            })
            if len(aggregate_rows) == api_total:
                complete_rows, complete_payload = list(aggregate_rows), recovery_payload
                break

    if complete_rows is None and len(aggregate_rows) > len(best_rows):
        best_rows = list(aggregate_rows)

    chosen_rows = complete_rows if complete_rows is not None else best_rows
    chosen_payload = complete_payload or best_payload or payload or {"success": True, "data": {}}
    expected_total = int(api_total or len(chosen_rows))
    complete = bool(expected_total > 0 and len(chosen_rows) == expected_total)

    data = chosen_payload.setdefault("data", {})
    if not isinstance(data, dict):
        chosen_payload["data"] = data = {}
    data["rows"] = chosen_rows
    # Preserve the API's expected universe instead of rewriting it to the short
    # number of rows received.
    data["totalCount"] = expected_total
    data["_pagination_debug"] = debug
    data["_pagination_best_rows"] = len(chosen_rows)
    data["_pagination_aggregate_rows"] = len(aggregate_rows)
    data["_pagination_api_total"] = expected_total
    data["_pagination_complete"] = complete
    data["_pagination_shortfall"] = max(0, expected_total - len(chosen_rows))
    data["_pagination_passes"] = passes
    return chosen_payload, final_status


def fetch_stock_detail(client: httpx.Client, ticker: str) -> tuple[dict, int]:
    return fetch_json_with_retries(client, STOCK_DETAIL_URL_TEMPLATE.format(ticker=ticker))


def fetch_stock_intraday(client: httpx.Client, ticker: str) -> tuple[dict, int]:
    return fetch_json_with_retries(client, STOCK_INTRADAY_URL_TEMPLATE.format(ticker=ticker))


def fetch_stock_gex(client: httpx.Client, ticker: str) -> tuple[dict, int]:
    return fetch_json_with_retries(client, STOCK_GEX_URL_TEMPLATE.format(ticker=ticker), retries=2)


def fetch_watchlists(client: httpx.Client) -> tuple[dict, int]:
    return fetch_json_with_retries(client, WATCHLISTS_URL)

def fetch_watchlist_dashboard(client: httpx.Client, watchlist_id: int | str) -> tuple[dict, int]:
    return fetch_json_with_retries(client, WATCHLIST_DASHBOARD_URL_TEMPLATE.format(watchlist_id=watchlist_id))

def fetch_sparklines(client: httpx.Client, symbols: str) -> tuple[dict, int]:
    return fetch_json_with_retries(client, SPARKLINES_URL_TEMPLATE.format(symbols=symbols))

def fetch_score_top_longs(client: httpx.Client) -> tuple[dict, int]:
    return fetch_json_with_retries(client, SCORE_TOP_LONGS_URL)

def fetch_score_backtest(client: httpx.Client) -> tuple[dict, int]:
    return fetch_json_with_retries(client, SCORE_BACKTEST_URL)

def fetch_score_basket_history(client: httpx.Client) -> tuple[dict, int]:
    return fetch_json_with_retries(client, SCORE_BASKET_HISTORY_URL)
