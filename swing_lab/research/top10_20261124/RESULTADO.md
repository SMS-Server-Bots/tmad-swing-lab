# TOP10 — primera semana y prueba neutral al mercado (24/11/2026)

**Semana 13–20/11** (listas reconstruidas con el mismo modelo congelado y sin datos posteriores a cada señal):
caída fuerte (SPY −1,9 % el 16/11, QQQ −5,1 % el 20/11). Todo lo corto ganó y todo lo largo perdió, también al azar
(RANDOM10: cortos +1,26 R, largos −0,84 R). TOP10_GBM_V1 pasó a casi todo largo desde el 16/11 y perdió (−0,53 R);
MAG_SIDE_V1 y SHORT_HIGHVOL_V1 iban cortos y ganaron (+0,66 R y +0,73 R). Frente al universo del mismo lado y día,
ninguna gana: −0,17, −0,28 y −0,03 R.

**V2 neutral al mercado** (`v2_exceso.py`): objetivo = R de la operación menos el R medio del universo ese día en ese
lado; walk-forward 45 días. Parecía haber algo (correlación de rango corto 0,19, t 2,3; las 10 peores −0,22 R, t −3,2),
pero era el efecto de los costes: en acciones con ATR bajo, el 0,2 % de costes resta mucho en R. Sin costes, la
correlación es −0,04 (largo) y 0,02 (corto): **ninguna capacidad de elegir acciones**.

**Conclusión.** Con los datos de TMAD, el resultado de cualquier lista a 5 sesiones lo decide la dirección del mercado.
Lo único útil a nivel de acción es la magnitud (y, por costes, preferir ATR alto). El marcador mide desde hoy el exceso
sobre el universo para no confundir mercado con acierto.
