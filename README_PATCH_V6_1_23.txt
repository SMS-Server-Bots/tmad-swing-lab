PATCH TMAD v6.1.22 -> v6.1.23

1. Aplicar sobre TMAD_Dual_Bot_OneClick_2 con APLICAR_PATCH_TMAD_V6_1_23.bat.
2. El aplicador copia únicamente los archivos declarados, valida SHA-256, integridad canónica y sintaxis del patch.
3. Publicar y desplegar desde _2 mediante PUBLICAR_Y_DESPLEGAR_TMAD.bat, ya sincronizado con v6.1.23. Ese flujo ejecuta la validación completa antes de crear commit, tag, release y desplegar.

No incluye ni modifica 02_CONFIG, 03_DATOS o 04_RUNTIME.
No requiere variables nuevas: el MARKET OUTCOME AUDITOR usa valores internos conservadores.

Tras el arranque, las consolas mostrarán:
- DATA: [AUDITOR FEED]
- ALERTS: [AUDIT] SOURCE/FUNNEL/PRECISION/ANTICIPATION/RECALL/GEX VALUE/MISSES/DRIFT/OUTPUT

Los archivos completos se guardan en:
03_DATOS/alerts_bot/output/market_outcome_audit/
TELEMETRÍA COMPLETA DE CONSOLA

La revisión final de v6.1.23 añade telemetría estructurada sin modificar la estrategia:

DATA BOT
- [CONFIG DATA]: horarios, deep, caps, strict total, fines de semana y stale peer.
- [DATA RUN START]: run, sesión, perfil, fecha de mercado, límites y flags de captura.
- [DATA RUN END]: duración, estado, cobertura Scanner, enriquecimiento, GEX, strikes, expiraciones, CORE/FULL y handoff.
- [DATA HEALTH]: generación, bootstrap, último Scanner/GEX, ACK de Alerts, pendientes y edad del heartbeat.
- [AUDITOR FEED]: deep Scanner/GEX, universo, histórico de precios, ZIP intradía, perfiles y madurez futura.
- [MAINT DATA]: ventana, duración, tamaño, caché, semanas retiradas y SQLite correctos/fallidos.
- [DATA ERROR DETAIL · LOCAL CONSOLE]: traceback local ampliado; Discord continúa compacto.

ALERTS BOT
- [CONFIG ALERTS] y [CONFIG AUDITOR]: gates, confirmaciones, ventanas, recencia y parámetros del auditor.
- [ALERTS STATE]: pipeline, generación, modo y último procesamiento.
- [ALERTS SYNC]: Data/Alerts, último Scanner, ACK, backlog y edad de heartbeats.
- [ALERTS EVIDENCE]: modelo activo, OOS, CORE/GEX y madurez de outcomes.
- [ALERTS BOOK/AUDIT]: posiciones LIVE/SHADOW y último estado del auditor.
- [ALERTS GENERATION START/END]: procesamiento completo de cada generación y duración.
- [CORE CALIBRATION START/END]: evidencia inicial, challenger, decisión y duración.
- [ALERTS SCAN START/END]: universo, GEX, gates, candidatos, entradas, cierres y auditor.
- [AUDIT STATUS/FUNNEL/PRECISION/ANTICIPATION/RECALL/GEX VALUE/MISSES/DRIFT/OUTPUT]: validación prospectiva completa.
- [MAINT ALERTS]: migración, optimize, lock pendiente, ventana, duración y tamaño de DB.
- [ALERTS ERROR DETAIL · LOCAL CONSOLE]: traceback local ampliado; Discord continúa compacto.

Con las consolas completas de Data y Alerts se puede localizar si una desviación procede de captura, cobertura, sincronización, CORE, GEX, timing, fusion, lifecycle, cartera o auditor.


REFUERZO BASADO EN LOGS REALES v6.1.22
- [DATA GEX QUALITY]: cobertura, calidad, causas y tickers afectados.
- [CORE BOOTSTRAP START/END]: intentos, overlap por ticker, errores y exclusiones.
- [RESEARCH SUMMARY]: candidatos FUSION teóricos separados de ENTER operativos.
- [CORE CALIBRATION SKIP]: motivo exacto por el que no se recalibra.
- [ALERTS SCAN END]: scope SHADOW_LEARNING/LIVE_VALIDATED y embudo de rechazos.
- [CONFIG ALERTS] muestra todos los gates LIVE efectivos: score, proyección diaria y confirmaciones.
- El gate LIVE real incluye score, proyección diaria y confirmaciones independientes. SHADOW continúa siendo aprendizaje no operativo.
