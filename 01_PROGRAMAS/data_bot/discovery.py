from collections import defaultdict, Counter

from config import (
    DISCOVERY_ENABLED,
    SEND_SCHEMA_CHANGE_ALERTS,
    DISCOVERY_BASELINE_SUMMARY_ONLY,
    DISCOVERY_MAX_FIELDS_IN_ALERT,
    DISCOVERY_ALERT_TYPE_CHANGES,
    DISCOVERY_AGGREGATE_ALERTS,
    DISCOVERY_COMMON_FIELD_MIN_RATIO,
    DISCOVERY_COMMON_FIELD_MIN_PAYLOADS,
    DISCOVERY_SUPPRESS_RARE_FIELDS,
)
from db_core import upsert_schema_field, insert_extra_field, count_schema_fields
from discord_utils import log_schema
from time_utils import ts_utc_str, ts_madrid_str
from utils import flatten_json

# Campos normalizados oficialmente. El raw JSON completo se guarda igualmente.
# Los campos que no estén aquí se guardan adicionalmente en extra_fields para research/adaptación.
KNOWN_PREFIXES = {
    'scanner': {
        'success',
        'data.rows[]',
        'data.rows[].ticker','data.rows[].symbol','data.rows[].name','data.rows[].category',
        'data.rows[].currentPrice','data.rows[].signalsAvailable','data.rows[].changePercent',
        'data.rows[].trend','data.rows[].momentumScore','data.rows[].rsi14',
        'data.rows[].optionSignal','data.rows[].totalGex','data.rows[].gexChange',
        'data.rows[].ivZSpread','data.rows[].ivChange','data.rows[].unusualFlowSignal',
        'data.rows[].otmCallVolumeZ','data.rows[].otmPutVolumeZ','data.rows[].pcRatio',
        'data.rows[].rvol','data.rows[].longScore','data.rows[].longScoreSignals',
        'data.totalCount','data.date','data.lastSyncTimestamp','data.lastSyncType',
        'data.featureFlags.longScoreEnabled',
        'data._pagination_debug[]','data._pagination_best_rows',
    },
    'detail': {
        'success','data.symbol','data.name','data.currentPrice','data.priceChange','data.priceChangePercent',
        'data.isLivePrice','data.dailyZones[]','data.dailyZones[].type','data.dailyZones[].top',
        'data.dailyZones[].bottom','data.dailyZones[].strength','data.dailyZones[].timeframe',
        'data.weeklyZones[]','data.weeklyZones[].type','data.weeklyZones[].top',
        'data.weeklyZones[].bottom','data.weeklyZones[].strength','data.weeklyZones[].timeframe',
        'data.optionSignal','data.putCallRatio','data.ivZSpread','data.momentumScore','data.trend',
        'data.history[]','data.history[].date','data.history[].signal','data.history[].trend',
        'data.history[].momentumScore','data.history[].ivZSpread',
    },
    'intraday': {
        'success','data.sessionStats.vwap','data.sessionStats.rsi','data.sessionStats.rvol',
        'data.keyLevels.nearestSupply','data.keyLevels.nearestDemand',
    },
    'gex': {
        'success','data.totalGex','data.gammaFlip','data.gexByStrike[]',
        'data.gexByStrike[].strike','data.gexByStrike[].gex','data.gexByStrike[].callGex',
        'data.gexByStrike[].putGex','data.gexByStrike[].callOI','data.gexByStrike[].putOI',
    },
}


def _field_line(path: str, typ: str) -> str:
    return f"- `{path}` ({typ})"


# Buffer global de discovery para evitar mandar 1 mensaje por ticker.
# Se vacía con flush_schema_alerts() al final de cada captura scanner/GEX.
_SCHEMA_BUFFER = defaultdict(lambda: {
    'payloads': 0,
    'baseline': False,
    'sample_tickers': set(),
    'observed_counts': Counter(),   # path -> payloads donde aparece
    'observed_types': {},           # path -> stable type
    'new_fields': {},               # path -> type, candidato; se filtra por presencia común en flush
    'type_changes': {},             # path -> set("old → new")
    'new_extra': {},                # path -> type, candidato; se filtra por presencia común en flush
})


def reset_schema_alert_buffer():
    _SCHEMA_BUFFER.clear()


def _is_common_field(endpoint_type: str, item: dict, path: str) -> bool:
    """Devuelve True si el campo parece común/estructural del endpoint.

    En endpoints por ticker (detail/intraday/gex), algunos campos aparecen solo cuando
    un ticker trae datos incompletos, news, gammaFlip, nulls, etc. Esos campos no deben
    disparar alertas de schema. El schema channel debe representar la estructura común.
    """
    payloads = max(int(item.get('payloads') or 0), 1)
    count = int(item['observed_counts'].get(path, 0))

    # Scanner suele ser un único payload con rows[]; si aparece en ese payload, es común.
    if endpoint_type == 'scanner':
        return True

    # Si hay muy pocos payloads, todavía no hay muestra suficiente para juzgar rareza.
    # En ese caso, solo avisa si aparece en todos los payloads vistos.
    if payloads < DISCOVERY_COMMON_FIELD_MIN_PAYLOADS:
        return count == payloads

    ratio = count / payloads
    return ratio >= DISCOVERY_COMMON_FIELD_MIN_RATIO


def _filter_common(endpoint_type: str, item: dict, fields: dict[str, str]) -> dict[str, str]:
    if not DISCOVERY_SUPPRESS_RARE_FIELDS:
        return fields
    return {path: typ for path, typ in fields.items() if _is_common_field(endpoint_type, item, path)}


def _queue_schema_event(endpoint_type: str, *, ticker: str | None, baseline: bool, observed_fields: dict[str, str], new_fields: list[tuple[str, str]], type_changes: list[tuple[str, str, str]], new_extra: list[tuple[str, str]], payload_count: int = 1):
    item = _SCHEMA_BUFFER[endpoint_type]
    item['payloads'] += payload_count
    if baseline:
        item['baseline'] = True
    if ticker:
        item['sample_tickers'].add(ticker)

    for path, typ in observed_fields.items():
        item['observed_counts'][path] += 1
        item['observed_types'].setdefault(path, typ)

    for path, typ in new_fields:
        item['new_fields'].setdefault(path, typ)
    for path, old_type, new_type in type_changes:
        item['type_changes'].setdefault(path, set()).add(f'{old_type} → {new_type}')
    for path, typ in new_extra:
        item['new_extra'].setdefault(path, typ)


def flush_schema_alerts(context: str | None = None):
    """Send one aggregated schema message after a complete scanner/GEX execution."""
    if not SEND_SCHEMA_CHANGE_ALERTS or not _SCHEMA_BUFFER:
        reset_schema_alert_buffer()
        return

    parts = []
    header = '🧭 **TMAD schema discovery summary**'
    if context:
        header += f' | `{context}`'
    parts.append(header)

    emitted = False
    limit = max(5, DISCOVERY_MAX_FIELDS_IN_ALERT)
    for endpoint_type in sorted(_SCHEMA_BUFFER.keys()):
        item = _SCHEMA_BUFFER[endpoint_type]
        common_new_fields = _filter_common(endpoint_type, item, item['new_fields'])
        common_new_extra = _filter_common(endpoint_type, item, item['new_extra'])
        type_changes = item['type_changes'] if DISCOVERY_ALERT_TYPE_CHANGES else {}
        baseline = item['baseline']

        # No emitas nada si solo hubo campos raros de tickers concretos.
        if not baseline and not common_new_fields and not type_changes:
            continue
        if baseline and DISCOVERY_SUPPRESS_RARE_FIELDS and not common_new_fields and not common_new_extra:
            # Si baseline existe pero no hubo campos comunes nuevos que reportar, evita ruido.
            continue

        emitted = True
        endpoint_lines = [f"\n**Endpoint:** `{endpoint_type}`"]
        endpoint_lines.append(f"Payloads inspected: `{item['payloads']}`")
        endpoint_lines.append(f"Common-field threshold: `{DISCOVERY_COMMON_FIELD_MIN_RATIO:.0%}`")
        if item['sample_tickers']:
            samples = ', '.join(f'`{x}`' for x in sorted(item['sample_tickers'])[:8])
            endpoint_lines.append(f"Ticker samples: {samples}")

        rare_new_count = max(0, len(item['new_fields']) - len(common_new_fields))
        rare_extra_count = max(0, len(item['new_extra']) - len(common_new_extra))

        if baseline and DISCOVERY_BASELINE_SUMMARY_ONLY:
            endpoint_lines.append("Baseline/common schema registered: `yes`")
            endpoint_lines.append(f"Common fields registered/new: `{len(common_new_fields)}`")
            if common_new_extra:
                endpoint_lines.append(f"Common extra/non-normalized fields stored: `{len(common_new_extra)}`")
                sample = [_field_line(path, typ) for path, typ in list(sorted(common_new_extra.items()))[:min(10, limit)]]
                endpoint_lines.append("Sample common extra fields:\n" + "\n".join(sample))
            if DISCOVERY_SUPPRESS_RARE_FIELDS and (rare_new_count or rare_extra_count):
                endpoint_lines.append(f"Suppressed ticker-specific/rare fields: `{rare_new_count}` new / `{rare_extra_count}` extra")
            endpoint_lines.append("Action: raw payload saved + common schema_registry updated. Main tables are not broken.")
        else:
            if common_new_fields:
                sample = [_field_line(path, typ) for path, typ in list(sorted(common_new_fields.items()))[:limit]]
                endpoint_lines.append("**Common new fields:**\n" + "\n".join(sample))
                if len(common_new_fields) > limit:
                    endpoint_lines.append(f"... y `{len(common_new_fields)-limit}` campos comunes nuevos más")
            if type_changes:
                tc_lines = []
                for path, changes in list(sorted(type_changes.items()))[:limit]:
                    tc_lines.append(f"- `{path}` ({'; '.join(sorted(changes))})")
                endpoint_lines.append("**Type changes:**\n" + "\n".join(tc_lines))
                if len(type_changes) > limit:
                    endpoint_lines.append(f"... y `{len(type_changes)-limit}` cambios de tipo más")
            if DISCOVERY_SUPPRESS_RARE_FIELDS and rare_new_count:
                endpoint_lines.append(f"Ignored ticker-specific/rare new fields: `{rare_new_count}`")
        parts.append("\n".join(endpoint_lines))

    if emitted:
        log_schema("\n".join(parts))
    reset_schema_alert_buffer()


def inspect_payload(endpoint_type: str, payload: dict | None, ticker: str | None = None, run_type: str | None = None, run_id: int | None = None, payload_hash: str | None = None):
    if not DISCOVERY_ENABLED or payload is None:
        return

    baseline_before = count_schema_fields(endpoint_type) == 0
    flat = flatten_json(payload)
    utc = ts_utc_str(); mad = ts_madrid_str()
    new_fields = []
    type_changes = []
    new_extra = []
    observed_fields = {}
    known = KNOWN_PREFIXES.get(endpoint_type, set())

    for path, value in flat.items():
        is_new, type_changed, old_type, new_type = upsert_schema_field(endpoint_type, path, value)
        observed_fields[path] = new_type
        if path not in known:
            insert_extra_field(utc, mad, endpoint_type, ticker, run_type, run_id, path, value, payload_hash)
            if is_new:
                new_extra.append((path, new_type))
        if is_new:
            new_fields.append((path, new_type))
        elif type_changed and DISCOVERY_ALERT_TYPE_CHANGES:
            type_changes.append((path, old_type, new_type))

    if not SEND_SCHEMA_CHANGE_ALERTS or not (new_fields or type_changes):
        return

    if DISCOVERY_AGGREGATE_ALERTS:
        _queue_schema_event(
            endpoint_type,
            ticker=ticker,
            baseline=baseline_before,
            observed_fields=observed_fields,
            new_fields=new_fields,
            type_changes=type_changes,
            new_extra=new_extra,
            payload_count=1,
        )
        return

    # Modo legacy: manda mensaje inmediatamente. No recomendado salvo debug puntual.
    limit = max(5, DISCOVERY_MAX_FIELDS_IN_ALERT)
    common_new_fields = dict(new_fields)
    common_new_extra = dict(new_extra)

    if baseline_before and DISCOVERY_BASELINE_SUMMARY_ONLY:
        parts = [f"🧭 **TMAD schema baseline registered**\nEndpoint: `{endpoint_type}`"]
        if ticker:
            parts.append(f"Ticker sample: `{ticker}`")
        parts.append(f"Fields registered: `{len(flat)}`")
        if common_new_extra:
            parts.append(f"Extra/non-normalized fields stored: `{len(common_new_extra)}`")
            sample = [_field_line(path, typ) for path, typ in list(common_new_extra.items())[:min(10, limit)]]
            parts.append("Sample extra fields:\n" + "\n".join(sample))
        parts.append("Action: raw payload saved + schema_registry updated. Main tables are not broken.")
        log_schema("\n".join(parts))
        return

    parts = [f"🟡 **TMAD schema change detected**\nEndpoint: `{endpoint_type}`"]
    if ticker:
        parts.append(f"Ticker sample: `{ticker}`")
    if common_new_fields:
        parts.append("**New fields:**\n" + "\n".join(_field_line(path, typ) for path, typ in list(common_new_fields.items())[:limit]))
    if type_changes:
        parts.append("**Type changes:**\n" + "\n".join(f"- `{path}` ({old_type} → {new_type})" for path, old_type, new_type in type_changes[:limit]))
    if len(common_new_fields) > limit:
        parts.append(f"... y {len(common_new_fields)-limit} campos nuevos más")
    if len(type_changes) > limit:
        parts.append(f"... y {len(type_changes)-limit} cambios de tipo más")
    log_schema("\n".join(parts))
