# Indicador «Liquidity Conditions» de tmad.app (19/11/2026)

Nuevo en la página News. Media de 4 factores públicos (peso 0,25 cada uno): volatilidad realizada de
Treasuries (IEF), crédito (LQD/HYG), VIX y dólar (UUP). Histórico diario 19/09/2025–18/11/2026
(294 días, `liq_raw.txt`, sacado del HTML de la página). Hoy −1,39: el nivel más «tenso» del año.

Prueba frente a SPY (cierres de la página de detalle + Scanner), t de Newey-West:

| Variable | 5 sesiones | 10 sesiones | 20 sesiones |
|---|---:|---:|---:|
| Nivel | t −0,70 | t −0,61 | t −1,30 |
| Cambio 5 días | t −0,53 | t −0,02 | t +1,64 |

- No anticipa al SPY. El cambio a 5 días se mueve con el SPY de los 5 días anteriores (correlación 0,62):
  describe lo que ya ha pasado.
- Si acaso es contrario: con liquidez < −0,5 el SPY subió +3,6 % en 20 sesiones (36 días, casi todos
  del episodio de marzo–abril de 2026, un solo episodio).
- Estar en SPY solo con liquidez > 0: +8,0 % frente a +10,6 % comprando y manteniendo (Sharpe 0,77 vs 0,67).
- Aviso: el histórico puede estar recalculado con normalización de toda la muestra (posible mirada al futuro),
  lo que favorecería al indicador; aun así no predice.
