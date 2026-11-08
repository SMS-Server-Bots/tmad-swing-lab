# Alerts Bot v6.1.9

## Capas

- CORE: ranking probabilístico sin target exacto.
- GRAPH: confirmación/propagación ETF-sector-leveraged.
- GEX_DAILY: discovery independiente, target por strike y timing por expiry.
- GEX_INTRADAY: ENTER/RETEST/WAIT.
- FUSION: estado operativo y lifecycle.
- MARKET_REGIME: main ETFs, breadth, VIX/VVIX, volatilidad y crédito.

## Memoria

Las features completas, señales, régimen y outcomes se guardan en
`03_DATOS/alerts_bot/db/archive/layers/YYYY-MM/layers_YYYY-MM-DD.zip`.
El ZIP puede recibir revisiones conforme maduran las sesiones futuras. SQLite live conserva
solo una cola compacta mientras el outcome está pendiente.

## Discord

- Alerts Log: estado/coordination/descanso.
- Calibration: métricas por capa y top del run.
- Alerts: lista compacta, compra, update y cierre.
- Research: descubrimientos y challengers.

## Salidas

Sin stop fijo por precio. Cierre por target GEX, reversión estructural, cambio general confirmado
o máximo cinco sesiones.

## Contrato operativo

- Estado global único persistente.
- Research y Live separados estrictamente.
- ERROR bloquea publicación de candidatos.
- Una generación se procesa una sola vez.
- Segundo arranque termina sin tocar el lock activo.
- CORE publica métricas de discovery y reglas deduplicadas por familia.

## Posiciones abiertas

`positions_overview.py` publica el libro LIVE abierto después de cerrar el ciclo completo del run. Puede desactivarse con `OPEN_POSITIONS_OVERVIEW_ENABLED=false`. El comando manual es `python main.py --once positions`.

El ticker se representa siempre como `**$TICKER** [CORE]` o `**$TICKER** [GEX]`. Una tesis CORE que utiliza GEX para target/timing sigue marcada como CORE; GEX se reserva para descubrimientos `GEX_STANDALONE`.


## v6.1.5 — reconciliación de runs reales

- Alerts consume únicamente Scanner con `status=success`; un run parcial no se anuncia como captura válida.
- `market_open` exige datos de la sesión actual y no transforma el cierre anterior en señales ENTER.
- El deep se materializa en modo histórico antes de reconciliar FULL, sin abrir ni actualizar posiciones.
- La caché CORE exige coincidencia exacta del calendario fuente/materializado.
- El reporte discovery conserva las métricas OOS reales.


## v6.1.9 — calendario estricto y calibración visible

- CORE solo materializa el calendario fuente publicado por Data.
- Las fechas locales obsoletas se podan junto con sus outcomes.
- El mover-discovery informa el progreso del walk-forward en consola.
