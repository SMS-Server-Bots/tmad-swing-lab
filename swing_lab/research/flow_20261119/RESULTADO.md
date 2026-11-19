# Straddle: movimiento real frente al que descuentan las opciones (19/11/2026)

Datos: IV ATM del mapa de calor diario (cierres del 10, 11 y 12/11, ZIP W46), precio a las 15:35 ET
(velas 1 min y Scanner intradía hasta el 18/11). IV < 10 % anual descartada (datos rotos, p. ej. CABA 1,5 %).
`straddle.py`, salida en `straddle_out.txt`.

Mercado en la ventana: SPY +1,4 % el 11/11 y −2,6 % el 18/11 (QQQ −2,3 %, IWM −3,5 %).

| Inicio | Decil 9 ATR: real ÷ implícito | Deciles 0–6 | P&L mediano straddle decil 9 (4 sesiones, misma IV) |
|---|---:|---:|---:|
| 10/11 | 0,68 | 0,65 | −23,6 % |
| 11/11 | 1,63 | 1,06 | +33,8 % |
| 12/11 | 1,26 | 1,06 | +8,7 % |

- Lo estable: los tres días, las acciones de más movimiento se mueven **más, en proporción a su IV**, que el resto.
  Coincide con la IV/RV baja de los deciles altos medida el 10/11.
- Lo inestable: el nivel. Si el mercado no se mueve (inicio 10/11) todos los straddles pierden ~20–25 %;
  con la caída del 12–18/11, los del decil 9 ganan. Son 3 días solapados y un único episodio de mercado.
- La selección de 75 del bot: 4 sesiones real/implícito 1,13; 5 sesiones 0,89. Sin señal propia.
- Sin costes: los diferenciales de opciones en estas acciones (IV ~110 %) pueden comerse el 5–10 % de la prima.

Conclusión: aún no hay ventaja demostrable. Medir de nuevo cuando haya ≥ 4 semanas de inicios
(con fases de mercado distintas) y probar la versión relativa: comprar decil 9 y vender deciles 0–2.
