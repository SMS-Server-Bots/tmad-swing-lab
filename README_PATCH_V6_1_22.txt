PATCH TMAD v6.1.21 -> v6.1.22

1. Extrae el ZIP en una carpeta separada.
2. Ejecuta APLICAR_PATCH_TMAD_V6_1_22.bat.
3. Objetivo por defecto: C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2.
4. Espera: APLICACION E INTEGRIDAD COMPLETAS.
5. Ejecuta PUBLICAR_Y_DESPLEGAR_TMAD.bat dentro de la carpeta _2.

El aplicador no modifica 02_CONFIG, 03_DATOS ni 04_RUNTIME.
El despliegue mantiene backup, validación del tag, validación de producción y rollback automático.

Primer arranque v6.1.22:
- Alerts procesa en orden los runs válidos que Data publicó mientras estaba caído.
- Research queda bloqueado hasta recuperar la sincronización.
- El mantenimiento SQLite pendiente se reintenta con cooldown y nunca bloquea el pipeline.
- No se pierden ni se recalculan las capturas válidas de Data.

Recomendación: publicar/desplegar después de que termine el deep actualmente en curso.
