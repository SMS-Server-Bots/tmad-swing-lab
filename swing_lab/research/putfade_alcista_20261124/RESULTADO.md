# Ejemplos de Raul/Alessio (24/11/2026): PUT FADE en día alcista (NVDA 23/11) y CALL SURGE en UUP

Datos: cierres canónicos del Scanner 22/07–20/11, acciones ≥ 5 $. Retorno frente a SPY, t por días (Newey-West).
Precios de NVDA confirmados con Massive: −14,0 % el 20/11 (el +31 % del 23/11 aún no está en los datos).

| Patrón | Casos | 1 sesión | 2 sesiones | 3 sesiones |
|---|---:|---:|---:|---:|
| PUT FADE en día alcista | 741 | −0,45 % (t −2,0) | −0,91 % (t −3,2) | −1,13 % (t −3,1) |
| PUT FADE en día > +3 % | 249 | −1,11 % (t −2,9) | −1,57 % (t −3,3) | −1,54 % (t −2,9) |
| Día > +3 % sin PUT FADE (control) | 9.002 | −0,71 % (t −3,3) | −0,95 % (t −3,2) | −1,01 % (t −2,8) |
| CALL SURGE en ETF con día plano (tipo UUP) | 206 | +0,17 % (t 0,7) | +0,35 % (t 1,0) | +0,51 % (t 1,5) |

- La corrección tras un día alcista existe, pero la tienen igual los días alcistas SIN PUT FADE. Lo que aporta el
  PUT FADE, comparado el mismo día: −0,13 a −0,65 pp (t −0,5 a −2,0), no significativo.
- Ojo: la reversión a corto ya falló fuera de muestra con 18 meses de OHLCV (research18).
- UUP tuvo CALL SURGE 5 veces desde julio (la última el 19/11); movimientos posteriores de ±0,3–1,4 %, sin dirección.
