# swing_lab — lista EOD de swings, seguimiento y salidas

Resultados y decisión: [REPORT_20261109.md](REPORT_20261109.md).

## Uso

```bash
pip install pandas numpy scikit-learn        # scikit-learn solo para research/
# Descargar los archivos de las Releases de tmad-dual-bot (necesita gh autenticado)
python -m swing_lab.run fetch --archives data
# Lista EOD de la última sesión disponible y registro en ledger/picks.csv
python -m swing_lab.run list  --archives data
# Actualizar posiciones (HOLD / WATCH / CLOSED por STOP o TIME_D2) y marcador
python -m swing_lab.run track --archives data
```

En el VPS se leen directamente las ZIP y la base viva, siempre en solo lectura:

```powershell
python -m swing_lab.run list  --data-root C:\TMAD\03_DATOS
python -m swing_lab.run track --data-root C:\TMAD\03_DATOS
```

o, de forma equivalente, `EJECUTAR_SWING_LAB_VPS.ps1 -DataRoot C:\TMAD\03_DATOS`.

## En el VPS (programado)

```powershell
cd C:\Users\Administrator\Desktop\tmad-swing-lab\swing_lab
powershell -ExecutionPolicy Bypass -File .\INSTALAR_SWING_LAB_VPS.ps1 -RunNow
```
Cada día laborable, a las 14:30 hora del VPS (23:30 en Madrid), deja `swing_lab_runtime\ULTIMA_LISTA.md` y `swing_lab_runtime\MARCADOR.md`.

## Qué produce cada día

- **Políticas en papel** (`PAPER_V1_REV`, `C5050`, `RANDOM`): Top-5 LONG y SHORT con seguimiento y salida.
- **MOVERS_V1**: las 20 acciones con más recorrido esperado mañana, con su P80 como referencia de stop. No da dirección.
- **GAP_FADE_V1**: a las 09:35, corto en acciones con hueco al alza de más de 3 volatilidades, cerrando al final del día. Se evalúa en papel cada día.

## Salidas

- `out/eod_list_<fecha>.md`: lista LONG y SHORT de la política candidata, con el stop orientativo.
- `ledger/picks.csv`: todas las selecciones de todas las políticas, con su huella y su modo (PROSPECTIVE o HISTORICAL).
- `ledger/positions.csv`: estado de cada posición (entrada, stop, días, salida, retorno neto).
- `out/scoreboard.md`: marcador por política y lado, y comprobación del criterio de promoción.

## Reglas

- Las políticas de `policy.py` están **congeladas**. Si se cambian, se crea una política nueva y su contador empieza de cero.
- Solo cuentan para la promoción las selecciones generadas antes de la apertura de la sesión de entrada (PROSPECTIVE).
- Modo papel: no envía órdenes ni publica en Discord.
