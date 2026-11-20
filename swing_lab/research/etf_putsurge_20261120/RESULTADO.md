# Hipótesis de Raul: PUT SURGE en ETF principales con CORE alcista → corrección (20/11/2026)

PUT SURGE = `otm_put_volume_z` ≥ 2 (regla de la web). Continuista = CORE `trend` ↑ y SPY/ETF arriba en 5 sesiones.
Datos con z disponibles: 1–9/06 (legacy) + 22/07–13/11 (ZIP semanales): 81 sesiones. `etf_putsurge.py`.

- **ETF principales (SPY, QQQ, IWM, DIA): solo 3 casos** (DIA 07/08, QQQ 08/10 y 09/10, este último con el
  mismo z repetido: en la práctica 2). En los 3 el ETF bajó a 2–5 sesiones (−0,4 % a −3,1 %). El patrón se ve,
  pero con 2–3 casos no se puede distinguir del azar: con CORE ↑ sin PUT SURGE, el ETF baja a 2 días el 68 % de las veces.
- **Todos los ETF (161), 72 casos:** frente a días ↑ sin PUT SURGE de la misma fecha, 1 sesión −0,04 % (t −0,1),
  3 sesiones −0,12 % (t −0,3), 5 sesiones −0,68 % (t −1,2). Va en la dirección de la hipótesis a 5 días, sin significación.
- Falta histórico: el z de opciones solo está guardado desde junio. Si Raul conserva su backtest original
  (fechas y fuente), es lo que permitiría ampliar la muestra.
