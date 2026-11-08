# TMAD — Dónde están los datos (revisión 09/11/2026)

## Flujo

```
VPS (Windows) 03_DATOS
 ├─ data_bot/db/live/*.db         Scanner, GEX, raw, meta. Retención LIVE de 10 días (LIVE_RETENTION_DAYS).
 │    └─ al cerrar día/semana ──> data_bot/db/archive/
 │          ├─ 2026-Wxx.zip                 EOD semanal (scanner + gex + meta + raw)
 │          ├─ intraday/intraday_YYYY-MM-DD.zip   snapshots horarios 09:35–15:35 ET
 │          └─ gex_profiles/gex_profiles_*.zip    perfil GEX diario
 ├─ alerts_bot/db/live/tmad_alerts_adaptive.db   11,7 GB + WAL 5,6 GB. Sin retención: es el único almacén
 │      completo de feature_observations, outcomes, layers, alertas y trades.
 └─ alerts_bot/db/archive/layers/layers_*.zip    capas diarias de Alerts

BACKUP_TMAD_DATA_GITHUB.ps1 ──> GitHub Releases de tmad-dual-bot
 ├─ tmad-data-archive-YYYY-MM    ZIP inmutables (verificados por SHA)    ACTUALIZADO (índice del 08/11)
 ├─ tmad-data-index              archive-index.json (245 entradas)        ACTUALIZADO
 ├─ tmad-data-latest             snapshot 7z de las .db mutables          PARADO desde el 27/09
 ├─ tmad-alerts-output-cold      salidas semanales de Alerts W30–W38      PARADO desde el 22/09
 └─ tmad-legacy-scanner-cold-v1  old_2: 23/05–10/06 + OHLCV dic-2024–jun-2026

Cloudflare R2 (maestro frío según el diseño) + catálogo en Supabase (tmad-production.tmad.archive_catalog)
 └─ catálogo del 13/07 al 21/09; última verificación el 22/09            PARADO
history_query_worker (VPS, 127.0.0.1:8787) ──Cloudflare Tunnel──> se registra en una función Edge de Supabase

SYNC_RUNTIME_DATA_TMAD.ps1 ──> rama runtime-data   estado + muestras (≤250 filas por tabla), diario
```

## Cobertura real por periodo

| Periodo | Qué hay | Dónde |
|---|---|---|
| dic-2024 → 10/06/2026 | **OHLCV diario del proveedor**, unos 1.400 tickers y unas 509.000 filas ticker-día (universo de mayo–junio de 2026: hay sesgo de supervivencia) | legacy `tmad_scanner_live.db` → `ticker_price_history` |
| 23/05 → 10/06 | Scanner EOD + snapshots (unas 15 sesiones) y GEX de junio | legacy |
| 11/06 → 21/07 | **Hueco: no hay Scanner en ninguna fuente remota** | — |
| 22/07 → 30/10 | Scanner EOD (63 sesiones), intradía hasta el 05/11, perfiles GEX y capas de Alerts hasta el 05/11 | Releases |
| 02/11 → 06/11 (W45) | ZIP semanal ya generado en el VPS, pendiente de subir | VPS |
| 22/07 → hoy | Histórico completo de Alerts (features, outcomes, 149 alertas/trades) | **solo VPS** (`tmad_alerts_adaptive.db`) |

## Riesgos detectados

1. **El snapshot mutable no se sube desde el 27/09.** La base de Alerts (11,7 GB), que es la única copia completa del aprendizaje y de los trades, no tiene backup remoto reciente.
2. **R2 y su catálogo en Supabase están parados desde el 22/09.** Hoy la fuente remota más completa son las Releases de GitHub.
3. **Disco del VPS al 10,7 % libre (WARNING) y WAL de Alerts en 5,6 GB.** La 6.2.36 ya tuvo que recuperar un WAL de 4,24 GB.
4. **La protección del histórico diario (6.2.68) no está desplegada.** En `main`, `rebuild_canonical_daily_history` sigue con un `DELETE` global.
5. El gestor de almacenamiento borra en local los ZIP con más de 45 días después de verificarlos en remoto. Es correcto, pero obliga a que las Releases sean completas.

## Rendimiento real del bot LIVE (open_trades, exportación completa de 149 filas)

116 operaciones cerradas entre el 24/07 y el 06/11 (66 días de entrada): **+0,11 % de media por operación, t = 0,34**. LONG: −0,28 % (77 operaciones). SHORT: +0,84 % (39). En 107 de 116 la salida es `PATTERN_REVERSAL`; de media, el mejor punto alcanzado fue +1,63 % y el peor −1,12 %.
