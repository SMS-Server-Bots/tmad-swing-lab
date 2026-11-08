# TMAD Dual Bot v6.1.9 — arquitectura de descubrimiento y memoria completa

## Objetivo

Detectar qué activos pueden realizar un movimiento relevante, explicar de dónde procede,
aterrizar el recorrido y el horizonte con GEX y decidir el momento de entrada sin imponer
una única regla rígida al Scanner.

## Flujo de decisión

1. **CORE rankea el universo**
   - Aprende de movimientos alcistas y bajistas de al menos 5% en un máximo de 5 sesiones.
   - El 5% es una etiqueta estadística, no un target operativo inventado.
   - Combina ranking transversal, modelo histórico, momentum, trend, flow, IV, RVOL,
     fortaleza relativa, mercado, sector y grafo.
   - Puede descubrir una oportunidad aunque no coincida una conjunción exacta de reglas.

2. **Régimen de mercado controla la política**
   - SPY, QQQ, IWM, DIA, breadth, HYG/LQD, VIX/VVIX y ETFs de volatilidad.
   - Una lectura aislada de VIX nunca cierra una posición.
   - Varias confirmaciones independientes pueden bloquear nuevos longs/shorts o cerrar
     posiciones por reversión general del mercado.

3. **Grafo ETF/sector propaga y confirma**
   - Main ETFs, ETFs sectoriales, leveraged/inverse, componentes y lead-lag.
   - Puede generar `GRAPH_PROPAGATION` antes de que el ticker active CORE.
   - Las relaciones correlacionadas cuentan como un bloque, no como confirmaciones falsas.

4. **GEX Daily descubre de forma independiente**
   - No depende de CORE y puede generar `GEX_STANDALONE`.
   - GEX by strike construye el mapa de precio: walls, flip, soporte, fricción y zona objetivo.
   - GEX by expiry construye el mapa temporal: short, mid y long positioning.
   - Clasifica intención: `DIRECTIONAL`, `HEDGING`, `FOMO`, `SQUEEZE`, `STABILIZING`,
     `DISTRIBUTION` o `UNCLEAR`.
   - Calcula asimetría reward/risk entre wall objetivo y wall de soporte.

5. **GEX Intraday decide el timing**
   - Estados: `ENTER`, `RETEST` o `WAIT`.
   - Usa flow, persistencia, cambios de GEX, migración de walls, perfil, VWAP y contexto.
   - No cambia por sí solo la dirección estructural.

6. **Fusión produce el estado operativo**
   - `DISCOVERED`, `EARLY`, `WATCH`, `READY`, `RETEST`, `ENTER`, `EXTENDED`, `CAPPED`,
     `DIVERGENCE`, `MARKET_BLOCKED`, `DETERIORATING`, `INVALIDATED`.
   - Una entrada requiere zona GEX suficiente, intención direccional/squeeze, score mínimo
     y timing `ENTER`.
   - CORE puro y propagación de grafo se conservan como watchlist sin inventar target.

## Gestión de posiciones

- Sin stop fijo por movimiento adverso aislado del precio.
- Cierre por:
  1. zona objetivo GEX;
  2. reversión confirmada de la tesis;
  3. cambio general de mercado confirmado;
  4. máximo 5 sesiones.
- `±5%` sigue utilizándose para aprender y medir CORE, no como salida universal.

## Memoria histórica por capas

Se guardan señales y features punto-en-tiempo de:

- CORE;
- GRAPH;
- GEX_DAILY;
- GEX_INTRADAY;
- FUSION;
- régimen general de mercado.

Cada observación conserva su outcome futuro: hit ±5%, target GEX, sesión de llegada,
MFE/MAE, retorno al cierre, estado de ejecución, intención GEX y régimen.

## ZIP y protección del servidor

### Data Bot

- ZIP semanal: cierres deep canónicos y bases completas.
- ZIP intradía diario: todos los runs no-deep.
- ZIP de perfiles GEX diarios: baseline + deltas por ticker.
- Los ZIP se abren, se prueba cada miembro, se ejecuta `PRAGMA integrity_check` y se
  comparan recuentos antes de purgar live.
- Payloads detallados permanecen pocos días en SQLite; el histórico compacto CORE se
  mantiene durante al menos dos años.
- Retención ZIP por defecto: ilimitada (`0`).

### Alerts Bot

- `layers_YYYY-MM-DD.zip` por sesión.
- Revisión 1 guarda features/señales/regímenes y libera el payload grande de SQLite.
- Una cola compacta conserva únicamente lo necesario para madurar outcomes.
- Al llegar nuevas sesiones, el mismo ZIP incrementa revisión y añade outcomes.
- Cuando el horizonte queda completo, se purga también la cola compacta.
- La huella criptográfica de los outcomes evita revisiones repetidas sin datos nuevos.

## Calibración acumulativa

- CORE: hit ±5%, precision@K, lift, calibración por bins, tiempo al movimiento, MFE/MAE,
  régimen y estado de oportunidad.
- Grafo: lift frente a CORE, acierto por ETF/relación y lead-lag.
- GEX Daily: hit de zona, error de target, wall alcanzado/migrado e intención.
- GEX Intraday: uplift real de `ENTER` frente a `WAIT` y error de timing.
- Fusión: calidad por estado (`READY`, `ENTER`, `EXTENDED`, etc.).
- Estados de modelo: `ACCUMULATING`, `CHALLENGER`, `VALIDATED`.

## Operación

- Arranque: una recuperación del último hito disponible, incluso fuera de horario.
- Después: lunes a viernes de 12:00 a 00:00, hora Madrid.
- Fuera de ventana y fines de semana: heartbeat y coordinación únicamente.
- No se recalibra ni se envían setups repetidos sin una nueva huella de datos.

## Normalización v6.1

### Máquina de estados

Una única máquina persistente gobierna todo Alerts:

```text
WAITING_DATA → BUILDING_CORE → CALIBRATING → SHADOW_READY/LIVE_READY → REST
                                      ↘ ERROR
ERROR → SHADOW_READY/LIVE_READY solo mediante recuperación explícita
```

El estado se guarda en `runtime_state` y en `pipeline_state_history` con generación, etapa, modo, error, modelo y timestamp.

### Idempotencia

La identidad de trabajo es:

```text
generation + data_fingerprint + pipeline_version
```

Una generación consumida no se reconstruye. Un `BLOCKED` espera una nueva generación o una acción operativa explícita.

### Contrato de canales

- Calibration explica modelos.
- Research presenta candidatos no accionables y READY pendientes de timing.
- Live recibe únicamente eventos del lifecycle operativo.
- ERROR bloquea Research y Live.

### Familias CORE

La selección de patrones deduplica condiciones por familia y por solapamiento Jaccard de observaciones. El objetivo es conservar patrones estructuralmente distintos, no múltiples thresholds de la misma variable.
