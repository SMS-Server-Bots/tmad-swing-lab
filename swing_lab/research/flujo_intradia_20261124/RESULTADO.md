# Flujo de opciones durante la sesión — primera lectura (24/11/2026)

8 sesiones (11–20/11), 3.316 observaciones acción-hora, 125 acciones (las ~70 de más movimiento previsto de cada día).
Entrada al spot de la captura; retorno frente a SPY. `flujo_intradia.py`, salida en `flujo_intradia_out.txt`.

| Señal (positivo = sesgo alcista) | Hasta | Correlación media | t | Días a favor |
|---|---|---:|---:|---:|
| Volumen calls vs puts OTM, acumulado | cierre | −0,045 | −0,9 | 3/8 |
| Volumen de la última hora | cierre | −0,047 | −1,5 | 3/8 |
| Cambio de GEX calls vs puts | cierre | +0,015 | +0,4 | 4/8 |
| Cambio de GEX calls vs puts | día siguiente | +0,079 | +1,6 | 4/7 |
| Cambio de OI OTM calls vs puts | día siguiente | −0,055 | −0,7 | 3/7 |

- Nada significativo con 8 días. Lo único que apunta a algo es el cambio de GEX → día siguiente (quintil alto − bajo
  +0,75 %, t 1,1). Con tan pocos días es compatible con el azar.
- SPY/QQQ/IWM: más volumen de calls los días 19–20/11, justo antes de las caídas; correlación −0,12. Sin muestra.
- NVDA no está en la captura horaria: la lista son las 75 de más movimiento previsto, no las más negociadas.

## Hipótesis congelada (prerregistro, 24/11/2026)
Se evaluará SOLO con sesiones posteriores al 20/11 (sin volver a mirar 11–20/11):
H1: `gex_imb` de la captura de las 15:00 ET → retorno del cierre al cierre del día siguiente frente a SPY.
    Medida: correlación de rangos por día; éxito si media > 0 con t ≥ 2 tras ≥ 20 sesiones nuevas.
H2: lo mismo con `vol_imb` (esperado: signo negativo, contrario).
