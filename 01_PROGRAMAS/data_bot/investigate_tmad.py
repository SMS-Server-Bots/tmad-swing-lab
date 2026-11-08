# investigate_tmad_site.py
# -*- coding: utf-8 -*-
r"""
TMAD Site/Data Point Investigator

Objetivo:
- Abrir tmad.app con tu tmad_session.json.
- Navegar la web por módulos funcionales: Watchlist, Scanner y Score.
- Hacer exploración suave de botones/tabs/enlaces internos.
- Capturar todas las llamadas /api/ reales que dispara la UI.
- Guardar:
  - HAR completo
  - requests/responses JSON
  - schemas/shapes
  - screenshots por sección
  - texto visible por sección
  - inventario de elementos clickable
  - resumen por módulo/endpoint/data point

Uso:
  .\.venv\Scripts\python.exe -u investigate_tmad_site.py

Instalación:
  .\.venv\Scripts\python.exe -m pip install playwright python-dotenv
  .\.venv\Scripts\python.exe -m playwright install chromium

Notas:
- Solo navega y hace clicks suaves.
- No hace POST/PUT/DELETE manuales.
- No imprime cookies/tokens.
- No modifica configuración.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://tmad.app"
ALLOWED_HOST = "tmad.app"
BLOCKED_HOSTS = {"tmad.news", "www.tmad.news"}
BLOCKED_TEXT_RE = r"(newsletter|newsletters|subscribe|tmad\.news)"
SESSION_FILE_DEFAULT = str(Path(__file__).resolve().parents[2] / "02_CONFIG" / "tmad_session.json")
OUT_ROOT_DEFAULT = str(Path(__file__).resolve().parents[2] / "03_DATOS" / "data_bot" / "output" / "tmad_site_research")

MODULES = {
    "watchlist": {
        "routes": [
            "/watchlist",
            "/watchlists",
            "/dashboard/watchlist",
            "/app/watchlist",
        ],
        "keywords": [
            "Watchlist", "watchlist", "Portfolio", "Alerts", "Favorites",
            "Add", "Remove", "Signal", "Ticker", "GEX", "Trend", "Score",
        ],
    },
    "scanner": {
        "routes": [
            "/scanner",
            "/dashboard/scanner",
            "/app/scanner",
            "/",
        ],
        "keywords": [
            "Scanner", "scanner", "Sort", "Filter", "GEX", "Signal", "Momentum",
            "Trend", "Long Score", "Unusual", "Flow", "Options", "Score",
        ],
    },
    "score": {
        "routes": [
            "/score",
            "/scores",
            "/dashboard/score",
            "/app/score",
            "/long-score",
        ],
        "keywords": [
            "Score", "Scores", "Long Score", "Momentum", "Signals", "Ranking",
            "Trend", "AI", "Bullish", "Bearish", "Quality",
        ],
    },
}

STOCK_ROUTES = [
    "/stock/{ticker}",
    "/stocks/{ticker}",
    "/ticker/{ticker}",
    "/dashboard/stock/{ticker}",
]

DEFAULT_TICKERS = ["SPY", "AAPL", "NVDA", "RBLX", "FTNT"]


@dataclass
class ApiRecord:
    when_utc: str
    module: Optional[str]
    page_url: Optional[str]
    source: str
    method: str
    url: str
    endpoint_key: str
    status: Optional[int] = None
    content_type: Optional[str] = None
    body_sha256: Optional[str] = None
    body_bytes: Optional[int] = None
    json_top_keys: Optional[List[str]] = None
    ticker: Optional[str] = None
    error: Optional[str] = None


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(s: str, max_len: int = 160) -> str:
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"[^A-Za-z0-9._=-]+", "_", s)
    return (s[:max_len].strip("_") or "unknown")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def infer_ticker_from_url(url: str) -> Optional[str]:
    m = re.search(r"/api/stock/([^/?#]+)", url)
    return m.group(1) if m else None


def endpoint_key(url: str) -> str:
    p = urlparse(url)
    path = p.path
    path = re.sub(r"/api/stock/[^/]+", "/api/stock/{ticker}", path)
    qs = parse_qs(p.query)
    if qs:
        return path + "?" + "&".join(f"{k}=..." for k in sorted(qs.keys()))
    return path


def json_top_keys(obj: Any) -> List[str]:
    if isinstance(obj, dict):
        return sorted([str(k) for k in obj.keys()])
    if isinstance(obj, list):
        return ["<list>"]
    return [type(obj).__name__]


def normalized_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def flatten_shape(obj: Any, max_fields: int = 800) -> Dict[str, str]:
    out: Dict[str, str] = {}

    def walk(v: Any, path: str) -> None:
        if len(out) >= max_fields:
            return
        out[path or "$"] = normalized_type(v)
        if isinstance(v, dict):
            for k, val in v.items():
                walk(val, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            if v:
                # Mezclar hasta 5 elementos para ver campos opcionales en arrays.
                samples = [x for x in v[:5] if x is not None]
                if not samples:
                    samples = [v[0]]
                for sample in samples:
                    walk(sample, f"{path}[]" if path else "[]")

    walk(obj, "")
    return out


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


class TmadInvestigator:
    def __init__(self, args):
        self.args = args
        self.out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.records: List[Dict[str, Any]] = []
        self.current_module: Optional[str] = None
        self.network_jsonl = self.out_dir / "api_requests.jsonl"
        self.ui_events_jsonl = self.out_dir / "ui_events.jsonl"
        self.endpoint_shapes: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.endpoint_counts: Dict[str, int] = defaultdict(int)
        self.endpoint_modules: Dict[str, set] = defaultdict(set)


    def is_allowed_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            if not host:
                return True
            if host in BLOCKED_HOSTS:
                return False
            return host == ALLOWED_HOST or host.endswith("." + ALLOWED_HOST)
        except Exception:
            return False

    def is_blocked_click_item(self, item: Dict[str, Any]) -> bool:
        text = " ".join([
            str(item.get("text") or ""),
            str(item.get("aria") or ""),
            str(item.get("href") or ""),
        ]).strip()
        if re.search(BLOCKED_TEXT_RE, text, re.I):
            return True
        href = item.get("href")
        if href and isinstance(href, str):
            if href.startswith("http") and not self.is_allowed_url(href):
                return True
        return False

    def log_event(self, event: Dict[str, Any]) -> None:
        event["when_utc"] = now_utc()
        append_jsonl(self.ui_events_jsonl, event)

    async def capture_response(self, response) -> None:
        url = response.url
        if "/api/" not in url:
            return

        rec = ApiRecord(
            when_utc=now_utc(),
            module=self.current_module,
            page_url=response.frame.url if response.frame else None,
            source="ui_network",
            method=response.request.method,
            url=url,
            endpoint_key=endpoint_key(url),
            status=response.status,
            content_type=response.headers.get("content-type"),
            ticker=infer_ticker_from_url(url),
        )

        try:
            text = await response.text()
            rec.body_bytes = len(text.encode("utf-8", errors="ignore"))
            rec.body_sha256 = sha256_text(text)

            obj = None
            if "json" in (rec.content_type or "").lower() or text.strip().startswith(("{", "[")):
                try:
                    obj = json.loads(text)
                    rec.json_top_keys = json_top_keys(obj)
                except Exception as e:
                    rec.error = f"json_parse_error: {e}"

            fname = f"{datetime.utcnow().strftime('%H%M%S_%f')}_{response.status}_{safe_filename(rec.endpoint_key)}.json"
            if obj is not None:
                write_json(self.out_dir / "responses" / (self.current_module or "unknown") / fname, obj)
                shape = flatten_shape(obj)
                write_json(self.out_dir / "shapes" / (self.current_module or "unknown") / fname.replace(".json", ".shape.json"), shape)

                # Merge endpoint shape
                for k, v in shape.items():
                    self.endpoint_shapes[rec.endpoint_key][k] = v
            else:
                p = self.out_dir / "responses" / (self.current_module or "unknown") / fname.replace(".json", ".txt")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text[:500_000], encoding="utf-8", errors="ignore")

            self.endpoint_counts[rec.endpoint_key] += 1
            if self.current_module:
                self.endpoint_modules[rec.endpoint_key].add(self.current_module)

        except Exception as e:
            rec.error = f"capture_response_error: {e}"

        d = asdict(rec)
        self.records.append(d)
        append_jsonl(self.network_jsonl, d)

    async def save_page_artifacts(self, page, module: str, label: str) -> None:
        safe = safe_filename(label)
        try:
            await page.screenshot(path=str(self.out_dir / "screenshots" / module / f"{safe}.png"), full_page=True)
        except Exception as e:
            self.log_event({"type": "screenshot_error", "module": module, "label": label, "error": str(e)})

        try:
            text = await page.locator("body").inner_text(timeout=3000)
            p = self.out_dir / "visible_text" / module / f"{safe}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text[:200_000], encoding="utf-8", errors="ignore")
        except Exception as e:
            self.log_event({"type": "text_capture_error", "module": module, "label": label, "error": str(e)})

        try:
            inventory = await self.clickable_inventory(page)
            write_json(self.out_dir / "clickable_inventory" / module / f"{safe}.json", inventory)
        except Exception as e:
            self.log_event({"type": "inventory_error", "module": module, "label": label, "error": str(e)})

    async def clickable_inventory(self, page) -> List[Dict[str, Any]]:
        js = """
        () => {
          const els = Array.from(document.querySelectorAll('a, button, [role="button"], [role="tab"], [data-state], [aria-label]'));
          return els.slice(0, 500).map((el, idx) => {
            const r = el.getBoundingClientRect();
            return {
              idx,
              tag: el.tagName,
              text: (el.innerText || el.textContent || '').trim().slice(0, 120),
              aria: el.getAttribute('aria-label'),
              role: el.getAttribute('role'),
              href: el.getAttribute('href'),
              target: el.getAttribute('target'),
              visible: !!(r.width && r.height),
              x: Math.round(r.x),
              y: Math.round(r.y),
              w: Math.round(r.width),
              h: Math.round(r.height),
              cls: (el.getAttribute('class') || '').slice(0, 120)
            };
          });
        }
        """
        return await page.evaluate(js)

    async def goto_route(self, page, module: str, route: str) -> bool:
        url = BASE_URL + route
        if not self.is_allowed_url(url):
            self.log_event({"type": "blocked_goto_external", "module": module, "url": url})
            return False
        self.current_module = module
        self.log_event({"type": "goto", "module": module, "url": url})
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(self.args.wait_ms)
            if not self.is_allowed_url(page.url):
                self.log_event({"type": "blocked_external_after_goto", "module": module, "requested": url, "landed": page.url})
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
                return False
            await self.save_page_artifacts(page, module, "route_" + route.strip("/").replace("/", "_") or "home")
            return True
        except Exception as e:
            self.log_event({"type": "goto_error", "module": module, "url": url, "error": str(e)})
            return False

    async def click_by_keywords(self, page, module: str, keywords: List[str], max_clicks: int) -> None:
        clicked = 0

        # Primero tabs/buttons visibles por texto keyword
        for kw in keywords:
            if clicked >= max_clicks:
                break
            selectors = [
                f"button:has-text('{kw}')",
                f"a:has-text('{kw}')",
                f"[role='tab']:has-text('{kw}')",
                f"[role='button']:has-text('{kw}')",
                f"text={kw}",
            ]
            for sel in selectors:
                if clicked >= max_clicks:
                    break
                try:
                    loc = page.locator(sel)
                    n = await loc.count()
                    for i in range(min(n, 3)):
                        if clicked >= max_clicks:
                            break
                        try:
                            el = loc.nth(i)
                            if await el.is_visible(timeout=1000):
                                txt = ""
                                try:
                                    txt = (await el.inner_text(timeout=1000)) or ""
                                except Exception:
                                    txt = ""
                                href = None
                                try:
                                    href = await el.get_attribute("href")
                                except Exception:
                                    href = None
                                if re.search(BLOCKED_TEXT_RE, " ".join([txt, href or ""]), re.I):
                                    self.log_event({"type": "skip_blocked_keyword_click", "module": module, "keyword": kw, "text": txt[:120], "href": href})
                                    continue
                                before_url = page.url
                                self.log_event({"type": "click_keyword", "module": module, "keyword": kw, "selector": sel, "index": i})
                                await el.click(timeout=4000)
                                clicked += 1
                                await page.wait_for_timeout(self.args.wait_ms)
                                if not self.is_allowed_url(page.url):
                                    self.log_event({"type": "external_navigation_blocked_after_click", "module": module, "from": before_url, "to": page.url})
                                    await page.goto(before_url if self.is_allowed_url(before_url) else BASE_URL, wait_until="domcontentloaded", timeout=30_000)
                                    await page.wait_for_timeout(1000)
                                    continue
                                await self.save_page_artifacts(page, module, f"click_{clicked}_{kw}")
                        except Exception as e:
                            self.log_event({"type": "click_keyword_error", "module": module, "keyword": kw, "selector": sel, "error": str(e)})
                            continue
                except Exception:
                    continue

        # Luego botones/tabs genéricos visibles, evitando acciones peligrosas
        if clicked < max_clicks:
            inventory = await self.clickable_inventory(page)
            candidates = []
            deny = re.compile(r"(delete|remove|logout|sign out|billing|subscribe|cancel|payment|newsletter|tmad\.news)", re.I)
            for item in inventory:
                text = " ".join([str(item.get("text") or ""), str(item.get("aria") or ""), str(item.get("href") or "")]).strip()
                if not item.get("visible"):
                    continue
                if deny.search(text) or self.is_blocked_click_item(item):
                    continue
                if item.get("tag") in ("BUTTON", "A") or item.get("role") in ("button", "tab"):
                    candidates.append(item)

            for item in candidates[: max(0, max_clicks - clicked)]:
                try:
                    # Click por coordenadas para evitar selectores frágiles
                    x = item["x"] + max(1, item["w"] // 2)
                    y = item["y"] + max(1, item["h"] // 2)
                    before_url = page.url
                    self.log_event({"type": "click_generic", "module": module, "item": item})
                    await page.mouse.click(x, y)
                    clicked += 1
                    await page.wait_for_timeout(self.args.wait_ms)
                    if not self.is_allowed_url(page.url):
                        self.log_event({"type": "external_navigation_blocked_after_generic_click", "module": module, "from": before_url, "to": page.url, "item": item})
                        await page.goto(before_url if self.is_allowed_url(before_url) else BASE_URL, wait_until="domcontentloaded", timeout=30_000)
                        await page.wait_for_timeout(1000)
                        continue
                    await self.save_page_artifacts(page, module, f"generic_click_{clicked}")
                except Exception as e:
                    self.log_event({"type": "click_generic_error", "module": module, "item": item, "error": str(e)})

    async def probe_candidate_routes(self, context) -> None:
        """
        Probes suaves de endpoints que podrían existir.
        """
        tickers = [t.strip().upper() for t in self.args.tickers.split(",") if t.strip()] or DEFAULT_TICKERS
        endpoints = [
            "/api/watchlist",
            "/api/watchlists",
            "/api/watchlist/items",
            "/api/scanner",
            "/api/scanner?limit=500&offset=0",
            "/api/score",
            "/api/scores",
            "/api/long-score",
            "/api/stock/{ticker}",
            "/api/stock/{ticker}/intraday?days=30",
            "/api/stock/{ticker}/intraday?days=365",
            "/api/stock/{ticker}/gex",
            "/api/stock/{ticker}/history",
            "/api/stock/{ticker}/gex/history",
            "/api/stock/{ticker}/flow",
            "/api/stock/{ticker}/options",
            "/api/stock/{ticker}/zones",
            "/api/stock/{ticker}/news",
        ]

        urls = []
        for ep in endpoints:
            if "{ticker}" in ep:
                for t in tickers:
                    urls.append(BASE_URL + ep.format(ticker=t))
            else:
                urls.append(BASE_URL + ep)

        seen = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        probe_jsonl = self.out_dir / "probe_requests.jsonl"
        for i, url in enumerate(urls, 1):
            try:
                self.log_event({"type": "probe", "index": i, "total": len(urls), "url": url})
                resp = await context.request.get(url, timeout=15_000)
                text = await resp.text()
                rec = ApiRecord(
                    when_utc=now_utc(),
                    module="probe",
                    page_url=None,
                    source="probe",
                    method="GET",
                    url=url,
                    endpoint_key=endpoint_key(url),
                    status=resp.status,
                    content_type=resp.headers.get("content-type"),
                    body_sha256=sha256_text(text),
                    body_bytes=len(text.encode("utf-8", errors="ignore")),
                    ticker=infer_ticker_from_url(url),
                )
                obj = None
                if "json" in (rec.content_type or "").lower() or text.strip().startswith(("{", "[")):
                    try:
                        obj = json.loads(text)
                        rec.json_top_keys = json_top_keys(obj)
                    except Exception as e:
                        rec.error = f"json_parse_error: {e}"

                fname = f"{datetime.utcnow().strftime('%H%M%S_%f')}_{resp.status}_{safe_filename(rec.endpoint_key)}.json"
                if obj is not None:
                    write_json(self.out_dir / "probe_responses" / fname, obj)
                    shape = flatten_shape(obj)
                    write_json(self.out_dir / "probe_shapes" / fname.replace(".json", ".shape.json"), shape)
                else:
                    p = self.out_dir / "probe_responses" / fname.replace(".json", ".txt")
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(text[:500_000], encoding="utf-8", errors="ignore")

                append_jsonl(probe_jsonl, asdict(rec))
            except Exception as e:
                append_jsonl(probe_jsonl, {
                    "when_utc": now_utc(),
                    "source": "probe",
                    "url": url,
                    "error": str(e),
                    "endpoint_key": endpoint_key(url),
                })

            await asyncio.sleep(self.args.probe_sleep)

    def build_summary(self) -> None:
        # Leer probe records también
        all_records = list(self.records)
        probe_jsonl = self.out_dir / "probe_requests.jsonl"
        if probe_jsonl.exists():
            for line in probe_jsonl.read_text(encoding="utf-8").splitlines():
                try:
                    all_records.append(json.loads(line))
                except Exception:
                    pass

        by_endpoint = defaultdict(list)
        by_module = defaultdict(list)
        for r in all_records:
            by_endpoint[r.get("endpoint_key") or endpoint_key(r.get("url", ""))].append(r)
            by_module[r.get("module") or "unknown"].append(r)

        lines = []
        lines.append("# TMAD Site/Data Point Investigation")
        lines.append("")
        lines.append(f"Generated: {now_utc()}")
        lines.append("")
        lines.append("## Modules explored")
        lines.append("")
        for m in MODULES:
            count = len(by_module.get(m, []))
            lines.append(f"- **{m}**: {count} API responses captured")
        lines.append("")
        lines.append("## Endpoints discovered/probed")
        lines.append("")
        lines.append("| Endpoint | Count | Statuses | Modules | Example |")
        lines.append("|---|---:|---|---|---|")
        for ep, rows in sorted(by_endpoint.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            statuses = sorted({str(r.get("status")) for r in rows})
            modules = sorted({str(r.get("module")) for r in rows if r.get("module")})
            example = rows[0].get("url", "")
            lines.append(f"| `{ep}` | {len(rows)} | {', '.join(statuses)} | {', '.join(modules)} | `{example}` |")

        lines.append("")
        lines.append("## Data point shapes by endpoint")
        lines.append("")
        for ep, shape in sorted(self.endpoint_shapes.items()):
            lines.append(f"### `{ep}`")
            lines.append("")
            for k, v in sorted(shape.items())[:250]:
                lines.append(f"- `{k}`: `{v}`")
            if len(shape) > 250:
                lines.append(f"- ... {len(shape)-250} more fields")
            lines.append("")

        lines.append("")
        lines.append("## Historical/API candidates")
        lines.append("")
        hist_terms = ("history", "date", "from", "to", "daily")
        for ep, rows in sorted(by_endpoint.items()):
            if any(t in ep.lower() for t in hist_terms):
                ok = sum(1 for r in rows if isinstance(r.get("status"), int) and 200 <= r.get("status") < 300)
                lines.append(f"- `{ep}` → {ok}/{len(rows)} successful")

        lines.append("")
        lines.append("## Output files")
        lines.append("")
        lines.append("- `tmad_site_capture.har`: HAR completo.")
        lines.append("- `api_requests.jsonl`: llamadas API capturadas navegando la UI.")
        lines.append("- `probe_requests.jsonl`: endpoints candidatos probados.")
        lines.append("- `responses/`: respuestas JSON reales por módulo.")
        lines.append("- `shapes/`: estructuras JSON por módulo.")
        lines.append("- `screenshots/`: capturas visuales por sección/click.")
        lines.append("- `visible_text/`: texto visible por sección/click.")
        lines.append("- `clickable_inventory/`: elementos clickables detectados.")
        lines.append("- `ui_events.jsonl`: navegación/clicks realizados.")

        (self.out_dir / "site_data_points_summary.md").write_text("\n".join(lines), encoding="utf-8")
        write_json(self.out_dir / "endpoint_shapes_merged.json", self.endpoint_shapes)

    async def run(self) -> None:
        session = Path(self.args.session)
        if not session.exists():
            print(f"[ERROR] No existe {session.resolve()}")
            sys.exit(1)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Output: {self.out_dir.resolve()}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.args.headless)
            context = await browser.new_context(
                storage_state=str(session),
                record_har_path=str(self.out_dir / "tmad_site_capture.har"),
                record_har_content="embed",
                viewport={"width": 1600, "height": 1000},
            )
            async def route_guard(route):
                req = route.request
                req_url = req.url

                # No bloquear assets externos genéricos porque pueden romper la app.
                # Solo bloqueamos navegaciones/documentos externos y navegación a tmad.news.
                if req_url.startswith("http") and not self.is_allowed_url(req_url):
                    if req.resource_type in ("document", "xhr", "fetch"):
                        self.log_event({
                            "type": "blocked_external_navigation_or_api",
                            "url": req_url,
                            "resource_type": req.resource_type,
                        })
                        await route.abort()
                        return

                await route.continue_()

            await context.route("**/*", route_guard)

            page = await context.new_page()

            async def close_popup(popup):
                # Importante: solo cerrar popups reales, nunca la página principal.
                try:
                    self.log_event({"type": "popup_closed", "url": popup.url})
                    await popup.close()
                except Exception:
                    pass

            page.on("popup", lambda popup: asyncio.create_task(close_popup(popup)))
            page.on("response", lambda response: asyncio.create_task(self.capture_response(response)))

            # Home inicial
            self.current_module = "home"
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                self.log_event({"type": "initial_goto_warning", "url": BASE_URL, "error": str(e)})
                # A veces la SPA aborta domcontentloaded por redirects internos; si la página sigue viva, continuamos.
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
            await page.wait_for_timeout(self.args.wait_ms)
            await self.save_page_artifacts(page, "home", "home")

            # Explorar módulos internos de tmad.app
            for module, cfg in MODULES.items():
                print(f"[MODULE] {module}")
                self.current_module = module

                reached = False
                for route in cfg["routes"]:
                    ok = await self.goto_route(page, module, route)
                    if ok:
                        reached = True
                        # explora clicks dentro del módulo
                        await self.click_by_keywords(page, module, cfg["keywords"], max_clicks=self.args.clicks_per_module)
                        break

                if not reached:
                    self.log_event({"type": "module_not_reached", "module": module})

            # Explorar algunos tickers
            tickers = [t.strip().upper() for t in self.args.tickers.split(",") if t.strip()] or DEFAULT_TICKERS
            self.current_module = "ticker_pages"
            for ticker in tickers:
                for route_t in STOCK_ROUTES:
                    route = route_t.format(ticker=ticker)
                    ok = await self.goto_route(page, "ticker_pages", route)
                    if ok:
                        await self.click_by_keywords(
                            page,
                            "ticker_pages",
                            ["GEX", "History", "Intraday", "Options", "Flow", "Zones", "News", "Score"],
                            max_clicks=self.args.clicks_per_ticker,
                        )
                        break

            if not self.args.skip_probes:
                print("[INFO] Probing candidate endpoints...")
                await self.probe_candidate_routes(context)

            await context.close()
            await browser.close()

        self.build_summary()

        print("")
        print("[OK] Investigación finalizada.")
        print(f"[OK] Resumen: {self.out_dir / 'site_data_points_summary.md'}")
        print(f"[OK] HAR:     {self.out_dir / 'tmad_site_capture.har'}")
        print("")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=SESSION_FILE_DEFAULT)
    ap.add_argument("--out", default=OUT_ROOT_DEFAULT)
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait-ms", type=int, default=2500)
    ap.add_argument("--clicks-per-module", type=int, default=25)
    ap.add_argument("--clicks-per-ticker", type=int, default=12)
    ap.add_argument("--skip-probes", action="store_true")
    ap.add_argument("--probe-sleep", type=float, default=0.20)
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(TmadInvestigator(args).run())
