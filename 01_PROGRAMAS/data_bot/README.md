# Data Bot v6.1.9

## Capas de almacenamiento

- SQLite live detallado: cola corta para operación diaria.
- Histórico CORE compacto: mínimo dos años.
- ZIP semanal deep: Scanner/GEX/raw/meta.
- ZIP intradía diario: runs horarios no-deep.
- ZIP de perfiles GEX: baseline + deltas strike×expiry.

## Seguridad ZIP

Antes de purgar live:

1. se crea un ZIP temporal;
2. se ejecuta `testzip`;
3. se extrae en temporal;
4. se ejecuta `PRAGMA integrity_check` en cada SQLite;
5. se comparan recuentos por tabla;
6. se sustituye atómicamente el ZIP final;
7. solo entonces se borran filas live.

Retención ZIP: ilimitada por defecto. Si el servidor se queda sin espacio, la política de
almacenamiento se resolverá externamente sin recortar los datos capturados.

## Coordinación

Data continúa siendo autoritativo para universo, excluidos, cobertura y generación. Alerts consume cada generación una sola vez mediante Coordination Bus.


## v6.1.5 — Scanner near-complete

La coincidencia exacta con el total API sigue siendo preferente. Una captura puede aceptarse como reconciliada únicamente si alcanza al menos `99,5%` de cobertura y faltan como máximo `3` tickers. El run se guarda como `success` con metadata de reconciliación; diferencias superiores permanecen `partial` y no se entregan a Alerts.
