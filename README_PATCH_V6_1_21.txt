PATCH TMAD v6.1.20 -> v6.1.21

1. Extrae el ZIP en una carpeta separada.
2. Ejecuta APLICAR_PATCH_TMAD_V6_1_21.bat.
3. El objetivo por defecto es C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2.
4. Espera: APLICACION E INTEGRIDAD COMPLETAS.
5. Ejecuta PUBLICAR_Y_DESPLEGAR_TMAD.bat dentro de la carpeta _2.

El aplicador no modifica 02_CONFIG, 03_DATOS ni 04_RUNTIME.
El despliegue mantiene backup, validación del tag, validación de producción y rollback automático.

En el primer arranque v6.1.21 se crean de forma idempotente:
- versiones/model cards CORE, GEX y EXPANSIÓN;
- ledgers locales de operaciones y evidencia;
- historial idempotente de trazabilidad ALERTA→CALIBRACIÓN.

30 sesiones sigue siendo el mínimo de calibración; el entrenamiento utiliza todo el histórico point-in-time acumulado dentro de la retención.
