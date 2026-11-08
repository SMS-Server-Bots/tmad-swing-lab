from __future__ import annotations

"""Point-in-time asset universe and explainable market relationship catalogue.

v5.6 treats every ticker present in the full TMAD Scanner as a graph node.
Static mappings are only priors. ETF/product names, category metadata and
point-in-time return histories create structural and dynamic edges. No inferred
relationship is treated as predictive until walk-forward validation proves it.
"""

import re
from typing import Iterable, Mapping, Any

CORE_MARKET_SYMBOLS = (
    "SPY", "QQQ", "IWM", "DIA", "SPX",
    "VIX", "VVIX", "VIXY", "VIXM", "VXZ", "VXX", "UVXY", "UVIX", "SVIX", "SVXY", "ZIVB",
    "TLT", "HYG", "LQD", "GLD", "SLV", "IBIT", "ETHA",
)

SECTOR_ETFS = (
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC",
    "SMH", "SOXX", "IGV", "HACK", "CIBR", "KBE", "KRE", "XBI", "IBB", "OIH", "XOP",
    "XRT", "XHB", "ITB", "JETS", "ITA", "XAR", "XME", "SLX", "GDX", "GDXJ", "TAN",
    "ARKK", "ARKG", "ARKF", "BOTZ", "ROBO", "URA", "COPX", "LIT", "REMX", "MJ",
    "IBIT", "ETHA",
)

# Parent -> (long leveraged, inverse/short). Generic name parsing supplements
# this catalogue and discovers single-stock products such as TSLL/NVDL/AAPU.
LEVERAGED_PAIRS: dict[str, tuple[str, ...]] = {
    "SPY": ("UPRO", "SPXL", "SPXS", "SPXU", "SH"),
    "QQQ": ("TQQQ", "QLD", "SQQQ", "QID", "PSQ"),
    "IWM": ("TNA", "UWM", "TZA", "TWM", "RWM"),
    "DIA": ("UDOW", "DDM", "SDOW", "DXD", "DOG"),
    "SMH": ("SOXL", "SOXS"),
    "SOXX": ("SOXL", "SOXS"),
    "XLF": ("FAS", "FAZ"),
    "KRE": ("DPST", "WDRW"),
    "XBI": ("LABU", "LABD"),
    "XLE": ("ERX", "ERY"),
    "XRT": ("RETL", "EMTY"),
    "ARKK": ("TARK", "SARK"),
    "GDX": ("NUGT", "DUST"),
    "GDXJ": ("JNUG", "JDST"),
    "TLT": ("TMF", "TMV", "TBT"),
    "VIX": ("VVIX", "UVIX", "UVXY", "VXX", "VIXY", "VIXM", "VXZ", "SVIX", "SVXY", "ZIVB"),
}

LEVERAGED_SYMBOLS = tuple(sorted({s for pair in LEVERAGED_PAIRS.values() for s in pair}))

SECTOR_PRIORS: dict[str, tuple[str, ...]] = {
    "technology": ("XLK", "QQQ"),
    "information technology": ("XLK", "QQQ"),
    "communication services": ("XLC", "QQQ"),
    "consumer discretionary": ("XLY", "SPY"),
    "consumer cyclical": ("XLY", "SPY"),
    "consumer staples": ("XLP", "SPY"),
    "consumer defensive": ("XLP", "SPY"),
    "financial": ("XLF", "SPY"),
    "financial services": ("XLF", "SPY"),
    "healthcare": ("XLV", "SPY"),
    "health care": ("XLV", "SPY"),
    "energy": ("XLE", "SPY"),
    "industrials": ("XLI", "SPY"),
    "industrial": ("XLI", "SPY"),
    "basic materials": ("XLB", "SPY"),
    "materials": ("XLB", "SPY"),
    "utilities": ("XLU", "SPY"),
    "real estate": ("XLRE", "SPY"),
}

INDUSTRY_PRIORS: dict[str, tuple[str, ...]] = {
    "semiconductor": ("SMH", "SOXX", "XLK", "QQQ"),
    "software": ("IGV", "XLK", "QQQ"),
    "cybersecurity": ("CIBR", "HACK", "XLK"),
    "bank": ("KBE", "XLF", "SPY"),
    "regional bank": ("KRE", "XLF", "SPY"),
    "biotech": ("XBI", "IBB", "XLV"),
    "pharmaceutical": ("IBB", "XLV"),
    "oil": ("XOP", "OIH", "XLE"),
    "gas": ("XOP", "XLE"),
    "retail": ("XRT", "XLY"),
    "homebuild": ("XHB", "ITB", "XLY"),
    "airline": ("JETS", "XLI"),
    "aerospace": ("ITA", "XAR", "XLI"),
    "defense": ("ITA", "XAR", "XLI"),
    "steel": ("SLX", "XME", "XLB"),
    "metal": ("XME", "XLB"),
    "gold": ("GDX", "GDXJ", "XLB"),
    "solar": ("TAN", "XLE"),
    "uranium": ("URA", "XLE"),
    "copper": ("COPX", "XME", "XLB"),
    "lithium": ("LIT", "XLB"),
    "rare earth": ("REMX", "XLB"),
    "cannabis": ("MJ", "XLP"),
    "robot": ("BOTZ", "ROBO", "XLK"),
    "fintech": ("ARKF", "XLF", "QQQ"),
}

TICKER_PRIORS: dict[str, tuple[str, ...]] = {
    "NVDA": ("SMH", "SOXX", "QQQ", "XLK"),
    "AMD": ("SMH", "SOXX", "QQQ", "XLK"),
    "AVGO": ("SMH", "SOXX", "QQQ", "XLK"),
    "TSM": ("SMH", "SOXX", "QQQ"),
    "MU": ("SMH", "SOXX", "QQQ"),
    "AAPL": ("QQQ", "XLK", "SPY"),
    "MSFT": ("QQQ", "XLK", "IGV", "SPY"),
    "GOOGL": ("QQQ", "XLC", "SPY"),
    "META": ("QQQ", "XLC", "SPY"),
    "AMZN": ("QQQ", "XLY", "SPY"),
    "TSLA": ("QQQ", "XLY", "ARKK"),
    "JPM": ("XLF", "KBE", "SPY"),
    "BAC": ("XLF", "KBE", "SPY"),
    "WFC": ("XLF", "KBE", "SPY"),
    "GS": ("XLF", "KBE", "SPY"),
    "XOM": ("XLE", "XOP", "SPY"),
    "CVX": ("XLE", "XOP", "SPY"),
    "MSTR": ("IBIT", "QQQ"),
    "COIN": ("IBIT", "ARKF", "QQQ"),
}

# Known single-stock products are a reliable prior; generic name parsing discovers
# further products directly from the current Scanner without needing a static list.
SINGLE_STOCK_PRODUCT_PARENTS: dict[str, str] = {
    "TSLL": "TSLA", "TSLQ": "TSLA", "TSLS": "TSLA",
    "NVDL": "NVDA", "NVDS": "NVDA", "NVDQ": "NVDA",
    "AAPU": "AAPL", "AAPD": "AAPL",
    "GGLL": "GOOGL", "GGLS": "GOOGL",
    "AMZU": "AMZN", "AMZD": "AMZN",
    "MSFU": "MSFT", "MSFD": "MSFT",
    "METU": "META", "METD": "META",
    "NFLU": "NFLX", "NFLD": "NFLX",
    "AMDL": "AMD", "AMDS": "AMD",
    "CONL": "COIN", "CONY": "COIN",
    "MSTU": "MSTR", "MSTX": "MSTR", "MSTZ": "MSTR",
}

_NEGATIVE_TOKENS = ("short", "bear", "inverse", "ultrashort", "-1x", "-2x", "-3x")
_POSITIVE_TOKENS = ("long", "bull", "ultra", "2x", "3x")
_ETF_TOKENS = (" etf", " etn", "fund", "shares", "trust")
KNOWN_INVERSE_PRODUCTS = {
    "TSLQ", "TSLS", "NVDS", "NVDQ", "AAPD", "GGLS", "AMZD", "MSFD",
    "METD", "NFLD", "AMDS", "MSTZ",
    "SPXS", "SPXU", "SH", "SQQQ", "QID", "PSQ", "TZA", "TWM", "RWM",
    "SDOW", "DXD", "DOG", "SOXS", "FAZ", "WDRW", "LABD", "ERY",
    "EMTY", "SARK", "DUST", "JDST", "TMV", "TBT", "SVIX", "SVXY",
}


def _norm(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("&", "and").split())


def _matches(value: str, catalogue: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    for fragment, etfs in catalogue.items():
        if fragment in value:
            return etfs
    return ()


def classify_asset(ticker: str, name: object = None, category: object = None) -> str:
    ticker = str(ticker or "").upper().strip()
    low_name = _norm(name)
    low_category = _norm(category)
    if ticker in {"VIX", "VIX1D", "VIX9D", "VVIX"}:
        return "volatility_index"
    if any(token in low_name for token in _NEGATIVE_TOKENS + _POSITIVE_TOKENS) and (
        "etf" in low_category or any(token in low_name for token in _ETF_TOKENS)
    ):
        return "leveraged_or_inverse_etf"
    if ticker in LEVERAGED_SYMBOLS or ticker in SINGLE_STOCK_PRODUCT_PARENTS:
        return "leveraged_or_inverse_etf"
    if "etf" in low_category or any(token in low_name for token in _ETF_TOKENS):
        return "etf"
    if "index" in low_category:
        return "index"
    if "stock" in low_category or "equity" in low_category:
        return "stock"
    return "other"


def leverage_polarity(name: object, ticker: str = "") -> float:
    low = _norm(name)
    ticker = str(ticker or "").upper().strip()
    if any(token in low for token in _NEGATIVE_TOKENS):
        return -1.0
    if ticker in KNOWN_INVERSE_PRODUCTS:
        return -1.0
    for parent, products in LEVERAGED_PAIRS.items():
        if ticker in products:
            return 1.0
    return 1.0


def leverage_multiple(name: object) -> float | None:
    low = _norm(name)
    match = re.search(r"(?<!\d)([123])\s*x", low)
    if match:
        return float(match.group(1))
    if "ultrapro" in low:
        return 3.0
    if "ultrashort" in low or "ultra " in low:
        return 2.0
    return None


def relationship_priors(ticker: str, sector: object = None, industry: object = None) -> list[str]:
    ticker = str(ticker or "").upper().strip()
    result: list[str] = []
    for symbol in TICKER_PRIORS.get(ticker, ()):
        if symbol not in result:
            result.append(symbol)
    for symbol in _matches(_norm(industry), INDUSTRY_PRIORS):
        if symbol not in result:
            result.append(symbol)
    for symbol in _matches(_norm(sector), SECTOR_PRIORS):
        if symbol not in result:
            result.append(symbol)
    if ticker in SECTOR_ETFS or ticker in CORE_MARKET_SYMBOLS:
        if ticker not in result:
            result.insert(0, ticker)
    if not result and ticker not in CORE_MARKET_SYMBOLS:
        result.extend(("SPY", "QQQ"))
    return result[:8]


def primary_sector_etf(ticker: str, sector: object = None, industry: object = None) -> str | None:
    for symbol in relationship_priors(ticker, sector, industry):
        if symbol in SECTOR_ETFS:
            return symbol
    return None


def _word_contains(name: str, token: str) -> bool:
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])", name.upper()))


def _explicit_single_stock_parent(name: object) -> str | None:
    """Extract only explicit underlying symbols from single-stock ETF naming.

    We deliberately avoid matching every uppercase word against stock tickers;
    that produced false links such as VXX→ETN or YINN→BULL.
    """
    text = str(name or "").upper()
    patterns = (
        r"\bDAILY\s+([A-Z]{1,6})\s+(?:BULL|BEAR)\b",
        r"\b(?:LONG|SHORT)\s+([A-Z]{1,6})\s+DAILY\b",
        r"\b(?:LONG|SHORT)\s+([A-Z]{1,6})\s+(?:TARGET|ETF)\b",
        r"\bSINGLE[- ]STOCK\s+([A-Z]{1,6})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def infer_structural_relationships(universe: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Infer point-in-time structural edges from the complete Scanner universe.

    Every node is classified. Leveraged/inverse ETF names are matched to an
    underlying ticker or parent ETF that is present in the same Scanner snapshot.
    The function is deterministic and does not use future returns.
    """
    symbols = {str(t).upper().strip() for t in universe if str(t).strip()}
    stock_symbols = {
        ticker for ticker, meta in universe.items()
        if classify_asset(ticker, meta.get("name"), meta.get("category")) == "stock"
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(ticker: str, related: str, relation_type: str, weight: float, polarity: float, metadata: dict) -> None:
        ticker = ticker.upper().strip(); related = related.upper().strip()
        if not ticker or not related or ticker == related or ticker not in symbols or related not in symbols:
            return
        key = (ticker, related, relation_type)
        if key in seen:
            return
        seen.add(key)
        rows.append({
            "ticker": ticker,
            "related_ticker": related,
            "relation_type": relation_type,
            "base_weight": max(0.05, min(1.0, float(weight))),
            "polarity": -1.0 if polarity < 0 else 1.0,
            "metadata": metadata,
        })

    # Broad/sector leveraged products.
    for parent, products in LEVERAGED_PAIRS.items():
        for product in products:
            if parent in symbols and product in symbols:
                meta = universe.get(product, {})
                polarity = leverage_polarity(meta.get("name"), product)
                multiple = leverage_multiple(meta.get("name"))
                add(product, parent, "leveraged_parent", 1.0, polarity, {"multiple": multiple, "source": "catalogue"})
                add(parent, product, "leveraged_confirmation", 0.90, polarity, {"multiple": multiple, "source": "catalogue"})

    # Single-stock products from reliable aliases, then generic name parsing.
    for product, parent in SINGLE_STOCK_PRODUCT_PARENTS.items():
        if product in symbols and parent in symbols:
            meta = universe.get(product, {})
            polarity = leverage_polarity(meta.get("name"), product)
            multiple = leverage_multiple(meta.get("name"))
            add(product, parent, "single_stock_parent", 1.0, polarity, {"multiple": multiple, "source": "catalogue"})
            add(parent, product, "single_stock_confirmation", 0.95, polarity, {"multiple": multiple, "source": "catalogue"})

    for product, meta in universe.items():
        asset_type = classify_asset(product, meta.get("name"), meta.get("category"))
        if asset_type != "leveraged_or_inverse_etf":
            continue
        name = str(meta.get("name") or "")
        parent = SINGLE_STOCK_PRODUCT_PARENTS.get(product) or _explicit_single_stock_parent(name)
        if parent and parent in stock_symbols and parent in symbols:
            polarity = leverage_polarity(name, product)
            multiple = leverage_multiple(name)
            add(product, parent, "single_stock_parent", 0.95, polarity, {"multiple": multiple, "source": "scanner_name"})
            add(parent, product, "single_stock_confirmation", 0.90, polarity, {"multiple": multiple, "source": "scanner_name"})

    return rows


def mandatory_relational_tickers(extra: Iterable[str] = ()) -> set[str]:
    return {
        *CORE_MARKET_SYMBOLS,
        *SECTOR_ETFS,
        *LEVERAGED_SYMBOLS,
        *(str(x).upper().strip() for x in extra if str(x).strip()),
    }
