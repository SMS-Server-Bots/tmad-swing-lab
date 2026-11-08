PATCH TMAD v6.1.23 -> v6.1.24

1. Extraer este ZIP en una carpeta separada de TMAD.
2. Ejecutar APLICAR_PATCH_TMAD_V6_1_24.bat.
3. Revisar RESULTADO_VALIDACION_V6_1_24.txt si la validación falla.
4. Desde TMAD_Dual_Bot_OneClick_2 ejecutar PUBLICAR_Y_DESPLEGAR_TMAD.bat.

El patch no modifica 02_CONFIG, 03_DATOS ni 04_RUNTIME. Las nuevas tablas se crean de forma idempotente dentro de la base de Alerts al arrancar.

v6.1.24 añade el Adaptive Learning Controller y corrige los problemas observados en los logs de v6.1.23. El aprendizaje automático queda restringido a SHADOW:
- puede pedir recalibración;
- evaluar challengers;
- adaptar el gate Research/Fusion dentro de límites;
- recalibrar probabilidades;
- seleccionar un modelo SHADOW;
- vigilarlo y revertirlo.

No puede:
- activar LIVE;
- cambiar el modelo LIVE;
- modificar el gate LIVE;
- editar código o configuración;
- borrar modelos históricos.

Flujo esperado en consola:
[AUDIT ...]
[LEARNING STATE]
[DRIFT DETECTOR]
[CHALLENGER FACTORY]
[CHALLENGER RESULT]
[THRESHOLD PROPOSAL]
[PROMOTION DECISION]

Al principio el estado será ACCUMULATING. La promoción SHADOW exige simultáneamente muestra prospectiva suficiente y un challenger seguro que supere la utilidad del champion.

V2 · PRIORIDAD OPERATIVA
Los logs completos del 05/08 mostraron una calibración de 6.155 segundos que dejó dos runs
Scanner pendientes y procesó el primero con 59 minutos de edad. Esta revisión evita ese bloqueo:
- la ventana de entradas y las tres horas previas tienen prioridad absoluta;
- cualquier run SUCCESS pendiente aplaza CORE learning;
- la generación se reconoce sin repetir handoffs;
- el aprendizaje queda en una cola persistente y se ejecuta después;
- los estados SKIP restauran el pipeline listo.

Marcadores adicionales:
[CORE LEARNING DEFERRED]
[LEARNING QUEUE]
[CORE CALIBRATION PERFORMANCE]

El objetivo no es hacer el optimizer concurrente con SQLite, sino diferirlo de forma segura para
que Alerts siga anticipando con datos frescos.

