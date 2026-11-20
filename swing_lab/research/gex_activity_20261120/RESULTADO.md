# «GEX Activity» de tmad.app: CALL/PUT SURGE y FADE (20/11/2026)

**Qué es.** No viene en la API: la web la calcula con `otmCallVolumeZ` / `otmPutVolumeZ` (volumen de opciones
fuera de dinero en desviaciones). SURGE si ≥ +2, FADE si ≤ −2. Comprobado con el histórico de ZSL (13 de 13 días
cerrados). El bot ya guarda ambos valores en `ticker_daily_history`, así que no hace falta cambiar la captura.

**Datos.** ZIP semanales W30–W46: 74 sesiones (22/07–13/11), 1.264 tickers ≥ 3 $, 62.408 filas con z.
Señales: CALL SURGE 5.972, PUT SURGE 5.488, CALL FADE 1.496, PUT FADE 1.082, ambas a la vez 1.352.
Retorno en exceso sobre la media del universo; t con un valor por día. `gex_activity.py`, salida en `gex_activity_out.txt`.

**Entrada realista: cierre del día siguiente a la señal** (el z se calcula con el volumen del día entero).

| Señal | 1 sesión | 5 sesiones | 10 sesiones |
|---|---:|---:|---:|
| CALL SURGE | −0,11 % (t −1,2) | −0,03 % (t −0,1) | +0,37 % (t 1,2) |
| PUT SURGE | +0,20 % (t 1,5) | +0,19 % (t 0,6) | +0,41 % (t 1,1) |
| CALL FADE | −0,01 % (t −0,1) | −0,46 % (t −1,4) | −0,31 % (t −0,6) |
| PUT FADE | −0,17 % (t −0,9) | **−0,66 % (t −2,3)** | −0,47 % (t −1,2) |

- **SURGE (lo que publica Alessio): nada.** CALL SURGE solo «funciona» entrando al cierre del mismo día
  (+0,22 % al día siguiente, t 2,1), que no se puede hacer porque la señal se conoce después del cierre.
- **PUT FADE: la única pista**, y débil. Siempre negativa (todas las ventanas y las dos mitades), pero
  jul–sep −0,96 % (t −2,7) y sep–nov −0,31 % (t −0,7): se apaga en la mitad reciente. Con 40 combinaciones
  probadas, un t de −2,3 es lo que cabe esperar por azar en 1–2 de ellas.
- Conclusión: no hay ventaja operable. PUT FADE como corto, solo en papel si se quiere seguir.
