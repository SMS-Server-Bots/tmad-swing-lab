# Parches para `tmad-dual-bot`

## `alpha_swing_engine_payload_and_timestop.patch`

Se aplica limpiamente sobre `main` 6.2.67 (`2abc023`): `git apply patches/alpha_swing_engine_payload_and_timestop.patch` desde la raíz de `tmad-dual-bot`.

### Bug 1: `payload_json` se anida a sí mismo y crece sin límite

`process_alpha_swing_book` guardaba en `event_payload["position"]` la fila completa, **incluido su `payload_json` anterior**. Cada ciclo del Scanner metía el JSON anterior, escapado, dentro del nuevo. Como cada nivel duplica las barras de escape, el crecimiento es exponencial.

Estado en la base LIVE el 10/11/2026:

| Fila | Estado | payload_json |
|---|---|---:|
| `alpha_swing_positions` id=1 RF SHORT | OPEN desde 25/09 | 889 MB |
| id=7 RIO SHORT | OPEN desde 04/11 | 881 MB |
| id=3 IBRX LONG | OPEN desde 12/10 | 769 MB |
| id=5 MPC LONG | CLOSED | 48 MB |
| `alpha_swing_events` (CLOSE/UPDATE de MPC) | — | 48 MB y 12 MB |

Consecuencias: en cada ciclo el bot carga, serializa y reescribe filas de unos 900 MB. Eso infla el WAL (5,25 GB), ocupa unos 2,6 GB de la base y alarga los backups.

**Arreglo**: `_position_snapshot()` copia la fila **sin** `payload_json`. `eod_discord.py` solo lee `target_price`, `invalidation_price` y `max_hold_sessions` de `position`, y se conservan.

### Bug 2: `TIME_STOP` nunca se dispara

`_sessions_seen` contaba solo las fechas de `price_cache` para el ticker. Si el ticker no está en `price_cache`, devuelve 0 para siempre (RF lleva 32 sesiones abierta con `sessions_seen=0` y `max_hold_sessions=5`).

**Arreglo**: devuelve el máximo entre el recuento de `price_cache` y las sesiones NYSE del calendario (festivos de 2026–2027 incluidos).

> ⚠️ **Efecto al desplegar**: RF, RIO e IBRX superan el máximo de 5 sesiones, así que en el primer ciclo se cerrarán por `TIME_STOP` y se enviarán **3 alertas CLOSE** a Discord.

### Orden recomendado

1. Desplegar el parche en `tmad-dual-bot` por el proceso normal.
2. En el VPS, compactar las filas ya infladas:
   ```powershell
   cd C:\Users\Administrator\Desktop\tmad-swing-lab\ops
   python .\compactar_alpha_swing_payload.py            # simulación
   python .\compactar_alpha_swing_payload.py --apply    # compacta
   ```
3. Al siguiente reinicio del bot de Alerts el WAL se trunca y se recuperan los ~5 GB. Las páginas liberadas dentro de la base (~2,6 GB) las reutiliza SQLite. Para encoger el fichero haría falta un `VACUUM` con el bot parado; es opcional.
4. Volver a ejecutar `INSTALAR_BACKUP_ALERTS_STREAM.ps1` después del despliegue, porque el instalador del backup original reactiva la tarea mutable antigua.

## data_bot_flow_capture.patch (11/11/2026)

Hace que el Data Bot guarde en el VPS lo que tmad.app publica sin histórico. Va a una base nueva,
`03_DATOS\data_bot\db\live\tmad_flow_live.db`, que entra en el ZIP semanal como `flow_<semana>.sqlite`, así que
sube al sistema de datos por el camino de siempre.

- **Mapas de calor de opciones**, todo el universo, cada día a las 12:10 (Madrid): cambio de OI, volumen, IV y
  cambio de GEX por contrato. ~10 MB/día.
- **Régimen del Score**, el mismo endpoint que alimenta el cuadro «Current Regime».
- **Velas de 1 minuto** con pre/post-market, los lunes y de forma incremental. ~250 MB la primera vez y luego
  ~50 MB/semana.
- **Arreglo del Score:** backtest y basket-history ahora dan 403 y ya no tumban la captura, así que el top-10
  vuelve a guardarse.

Corre en un hilo aparte y no retrasa el Scanner ni el GEX. Se ha comprobado que el parche aplica limpio sobre
`main` (v6.2.67) y que pasa `validation_support/validar_flow_capture.py`.

Aplicar en tmad-dual-bot:
```
git am C:\Users\Administrator\Desktop\tmad-swing-lab\patches\data_bot_flow_capture.patch
```
Prueba manual en el VPS, tras desplegar: `python main.py --once flow`.
