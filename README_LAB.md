# TMAD Swing Lab

Copia de trabajo de `SMS-Server-Bots/tmad-dual-bot` (snapshot de `main`, versión 6.2.67, commit `2abc023`) para calibrar el sistema de swings EOD sin solaparse con el repositorio original.

## Objetivo

1. **Lista EOD de swings**: cada cierre, Top-N LONG y SHORT con probabilidad, horizonte y nivel de invalidación.
2. **Alertas**: publicar esa lista.
3. **Seguimiento**: estado diario de cada posición abierta (HOLD / TRIM / CLOSE).
4. **Salida**: reglas de salida calibradas con trayectorias reales (entrada 09:35 ET del día siguiente, trayectoria horaria intradía, costes).

## Reglas de esta copia

- Los workflows que despliegan a Discord o sincronizan versiones están en `.github/workflows_disabled/`: esta copia **no** publica nada ni toca el VPS.
- Los datos se leen de las Releases de `tmad-dual-bot` (`tmad-data-index`, `tmad-data-archive-*`). No se suben datos crudos a este repositorio.
- Todo el trabajo nuevo vive en `swing_lab/`.
