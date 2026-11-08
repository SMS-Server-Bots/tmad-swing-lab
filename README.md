> **Continuation / session handoff:** see [`TMAD_CONTINUATION.md`](TMAD_CONTINUATION.md) before making the next strategic TMAD change.

# TMAD Dual Bot v6.1.9

## Objetivo de esta versión

La v6.1.9 convierte Discord en un panel auditable y elimina las ambigüedades observadas en los mensajes reales de la v6.1.5:

- el canal Alerts Live recibe siempre un overview central de posiciones, con libros LIVE y SHADOW separados;
- si no hay posiciones, se publica un estado vacío explícito en lugar de dejar el canal silencioso;
- el overview también se envía después de la recuperación/calibración de arranque;
- Research identifica la versión TMAD y el modelo CORE exacto (`#id` + clave);
- Calibration identifica tanto el código ejecutado como el modelo base/nuevo;
- la cobertura GEX diferencia lo materializado en Alerts de lo ya disponible en Data;
- los cierres Deep de Data se publican compactos, sin diccionarios Python ni rutas completas;
- se mantiene la garantía de consola + log local + webhook con recibo `delivery_id`.

Se conservan la reconciliación del calendario CORE, el Scanner near-complete, la separación temporal de Research y todos los gates fail-closed.

## Estado global único

Alerts solo puede estar en uno de estos estados:

- `WAITING_DATA`
- `BUILDING_CORE`
- `CALIBRATING`
- `SHADOW_READY`
- `LIVE_READY`
- `REST`
- `ERROR`

Cada transición queda persistida en SQLite. Un error no puede quedar oculto por una calibración posterior: la recuperación debe registrar explícitamente `ERROR → SHADOW_READY` o `ERROR → LIVE_READY`.

## Separación y orden de Discord

- **Data Log:** Scanner → GEX → archivo → cobertura → handoff.
- **Alerts Log:** arranque → Data/Alerts → CORE/outcomes → calibración → READY/ERROR.
- **Calibration:** estado del pipeline → CORE → GEX → grafo/fusión → backtest → configuración efectiva.
- **Research:** ENTER preliminar → READY/RETEST → GEX EARLY → CORE EARLY/WATCH → propagación ETF/grafo.
- **Live:** cierres objetivo → cierres invalidados → deterioros → seguimientos favorables → nuevas entradas.

Los bloques largos se dividen por tarjetas completas y repiten el título con `Parte i/n`; ya no se cortan por caracteres ni usan cabeceras repetitivas `[1/n]`. Que un ticker aparezca en Research no significa que exista una operación.

## Métricas por capa

### CORE

CORE mide discovery, no una estrategia completa:

- Hit ±5% en ≤5 sesiones.
- Lift frente a la tasa base.
- Precision@K.
- MFE y MAE.
- Tiempo medio hasta el movimiento.
- Resultado por régimen.

PF, expectancy y drawdown quedan como métricas técnicas del sistema fusionado, no como titulares de CORE.

### Grafo

- Lift sobre CORE.
- Lead-lag.
- Acierto por ETF/relación.

### GEX

- Acierto de la zona objetivo.
- Error de target y timing.
- Wall alcanzado o migrado.
- Intención GEX.

## Reglas CORE

Los thresholds equivalentes se agrupan por familias semánticas, por ejemplo:

- `VOLATILITY_PARTICIPATION`
- `MARKET_REGIME`
- `SECTOR_RELATIVE_STRENGTH`
- `OPTIONS_FLOW`
- `MOMENTUM_TREND`
- `PRICE_EXTENSION_REVERSAL`

No se publican cuatro reglas distintas solo porque ATR use cuatro cortes próximos.

## Seguridad operativa

- Una generación Data se procesa una sola vez.
- Un resultado `PROCESSED` o `BLOCKED` no reconstruye CORE de nuevo.
- Un segundo arranque de Alerts se cierra limpiamente y no elimina el lock del proceso activo.
- Research y Live permanecen bloqueados mientras el pipeline esté en `ERROR`, `WAITING_DATA`, `BUILDING_CORE` o `CALIBRATING`.
- Fuera de L–V 12:00–00:00 Madrid, el sistema entra en `REST` y mantiene heartbeat/coordinación.

## Datos y ZIP

Se conserva toda la funcionalidad y todo el histórico:

- Data semanal: `03_DATOS/data_bot/db/archive/YYYY-Www.zip`
- Data intradía: `03_DATOS/data_bot/db/archive/intraday/YYYY-MM/intraday_YYYY-MM-DD.zip`
- Perfiles GEX: `03_DATOS/data_bot/db/archive/gex_profiles/YYYY-MM/gex_profiles_YYYY-MM-DD.zip`
- Capas Alerts: `03_DATOS/alerts_bot/db/archive/layers/YYYY-MM/layers_YYYY-MM-DD.zip`

Los ZIP se verifican antes de purgar filas live. La retención es ilimitada por defecto.

## Instalación

1. Detener Data y Alerts.
2. Descomprimir el contenido sobre `TMAD_Dual_Bot_OneClick` y sustituir archivos.
3. Conservar intactos `02_CONFIG`, `03_DATOS` y `04_RUNTIME`.
4. Ejecutar `VALIDAR_TMAD_V6_1_9.bat`.
5. Iniciar con `INICIAR_TMAD_DUAL_BOT.bat`.

## v6.1.4 — overview de posiciones

Después de cada run operativo, Alerts publica un resumen final de todas las posiciones LIVE abiertas. Se ordena por atención y usa:

- 🔴 tesis a una confirmación adversa del cierre;
- 🟡 cambio relevante todavía no invalidante;
- 🟢 avance confirmado;
- ⚪ sin cambios relevantes.

El contrato visual de ticker es único en Alerts: `**$TICKER** [CORE]` o `**$TICKER** [GEX]`.


## v6.1.5 — robustez de runs y coherencia temporal

- Alerts consulta exclusivamente `scanner_runs.status='success'`.
- Los runs `running`, `partial`, `partial_base_saved`, `interrupted` o `failed` no generan errores operativos ni informes Research.
- La apertura usa únicamente Scanner de la fecha actual; el premarket sí puede usar el cierre anterior.
- Un informe de apertura en espera usa una clave de deduplicación distinta, de modo que el informe fresco posterior todavía se publica.
- La reconciliación FULL incorpora primero el deep autoritativo y después calcula filas pendientes.
- La validación específica reproduce los casos reales `972/974`, stale market-open y OOS `39/20`.


## v6.1.6 — calendario CORE autoritativo y progreso

- La materialización usa exclusivamente las fechas publicadas por Data Bot.
- Se eliminan observaciones y outcomes locales que no pertenecen a ese calendario.
- El primer arranque puede mostrar una poda única; los siguientes deben quedar con `drift_fechas=0`.
- La consola informa preparación de muestra, total de folds, avance cada cinco folds, generación de reporte y persistencia del modelo.
- El heartbeat de Alerts permanece independiente durante el cálculo.


## Garantía de entrega Discord

Desde v6.1.8, cada tarjeta editorial generada se imprime, se guarda localmente y se envía al webhook de su canal. Los mensajes técnicos de alta frecuencia siguen únicamente en consola para no saturar Discord. Consulta `ORDEN_CANALES_DISCORD_V6_1_9.md`.


## v6.1.7 — limpieza editorial de Discord

- Cada canal tiene una función única: estado, calibración, Research u operativa.
- Las shortlists muestran «Mostradas X de Y».
- Los tickers usan `• **$TICKER** [CORE/GEX/GRAFO]`.
- Los diagnósticos técnicos extensos permanecen en archivos locales.

## v6.1.8 — entrega Discord garantizada

- Toda tarjeta editorial generada se imprime, se guarda y se envía por webhook.
- Cada entrega correcta o fallida deja un recibo local con `delivery_id`.
- Los flags heredados ya no pueden dejar una tarjeta generada solo en consola.

## v6.1.9 — dashboard y versiones auditables

- Alerts Live recibe el dashboard consolidado LIVE + SHADOW, incluido el estado vacío.
- El dashboard se publica después de cada run y después de la recuperación de arranque.
- Research y Calibration muestran TMAD, id de modelo y clave de calibración.
- GEX diario muestra por separado cobertura Alerts y cobertura Data.
