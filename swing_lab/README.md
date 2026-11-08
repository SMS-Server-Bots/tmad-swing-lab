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

## Salidas

- `out/eod_list_<fecha>.md`: lista LONG y SHORT de la política candidata, con el stop orientativo.
- `ledger/picks.csv`: todas las selecciones de todas las políticas, con su huella y su modo (PROSPECTIVE o HISTORICAL).
- `ledger/positions.csv`: estado de cada posición (entrada, stop, días, salida, retorno neto).
- `out/scoreboard.md`: marcador por política y lado, y comprobación del criterio de promoción.

## Reglas

- Las políticas de `policy.py` están **congeladas**. Si se cambian, se crea una política nueva y su contador empieza de cero.
- Solo cuentan para la promoción las selecciones generadas antes de la apertura de la sesión de entrada (PROSPECTIVE).
- Modo papel: no envía órdenes ni publica en Discord.
