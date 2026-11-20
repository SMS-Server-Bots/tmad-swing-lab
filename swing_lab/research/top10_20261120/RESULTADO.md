# Sistema TOP10 — primer backtest walk-forward (20/11/2026)

**Datos.** Fotos canónicas de cierre del Scanner 22/07–13/11 (72.591 filas, 81 % con z de opciones, 91 % con muros),
precios intradía horarios hasta el 19/11 para entrada, máximos y mínimos. Almacén `store.py` (114 variables),
etiquetas `labels.py`, modelo `top10.py`.

**Criterio de éxito (Raul).** Entrada en la apertura siguiente; acierto si toca objetivo 1,5·ATR antes que el stop 1·ATR,
máx. 5 sesiones; si toca los dos el mismo día cuenta como stop; costes 0,2 %. Tasa base del universo: largo 33 % objetivo /
55 % stop; corto 39 % / 48 %.

**Walk-forward** (reentreno cada 5 sesiones, purga de 6 sesiones, calibración isotónica fuera de muestra): 41 días, 03/09–13/11.

| Estrategia | R por operación | t (NW) | Objetivo | Stop | 03/09–08/10 | 09/10–13/11 |
|---|---:|---:|---:|---:|---:|---:|
| TOP10 modelo (EV) | −0,20 | −2,75 | 30 % | 58 % | −0,06 | −0,32 |
| Azar (acción y lado) | −0,09 | −1,88 | 38 % | 56 % | −0,08 | −0,01 |
| Magnitud + lado al azar | −0,05 | −1,36 | 42 % | 54 % | −0,13 | +0,04 |
| Magnitud + lado del modelo | +0,01 | +0,14 | 45 % | 52 % | +0,00 | +0,02 |
| Cortos en las 10 de más ATR | +0,29 | +2,03 | 39 % | 37 % | +0,00 | +0,55 |

- El modelo **no** tiene ventaja y su calibración no es monótona: los casos con probabilidad prevista más alta salen peor
  (sobreajuste de los extremos con solo ~60 sesiones de entrenamiento).
- Cortos en las más volátiles: todo el resultado viene de la caída del 09/10–13/11. Es régimen, no ventaja demostrada.
- La magnitud sí se elige bien (con «magnitud» el objetivo se toca más: 42–45 % frente a 38 %), pero el lado no.

**Fallo encontrado en el Data Bot:** `ticker_daily_history` perdía los muros del GEX en los días sin reinicio
(PR #183, 6.2.75). El laboratorio lee ahora las fotos canónicas de cierre.

**En papel desde hoy (VPS, tarea nocturna):** TOP10_GBM_V1, MAG_SIDE_V1, SHORT_HIGHVOL_V1 y el control RANDOM10,
con lista congelada, seguimiento y marcador (`ledger/top10_*.csv`, `ULTIMA_LISTA.md`, `MARCADOR.md`).
