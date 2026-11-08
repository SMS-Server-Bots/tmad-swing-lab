PATCH TMAD v6.1.19 -> v6.1.20

OBJETIVO
Actualizar el repositorio de trabajo:
C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2

INSTALACIÓN
1. Extrae el ZIP del patch en cualquier carpeta; no lo extraigas encima de TMAD.
2. Ejecuta APLICAR_PATCH_TMAD_V6_1_20.bat.
3. El aplicador crea backup temporal, copia únicamente los archivos del patch, elimina artefactos versionados v6.1.19, verifica todos los SHA-256 y ejecuta controles rápidos de integridad/compilación.
4. Cuando aparezca APLICACION E INTEGRIDAD COMPLETAS, ejecuta en la carpeta _2:
   PUBLICAR_Y_DESPLEGAR_TMAD.bat

VALIDACIONES DEL FLUJO OFICIAL
- El aplicador no repite las 29 suites completas: evita una cuarta validación redundante.
- El publicador valida el repositorio local antes de crear commit/tag/release.
- El desplegador valida el paquete exacto reconstruido desde el tag de GitHub.
- Después valida de nuevo producción y solo entonces arranca los bots.
- Ante cualquier fallo en producción, ejecuta rollback automático.

SEGURIDAD DE DATOS
- El patch no contiene ni modifica .env, 02_CONFIG, 03_DATOS o 04_RUNTIME.
- La migración legacy se ejecuta al arrancar Alerts sobre la base existente, sin reabrir operaciones ni borrar resultados.
- El mantenimiento semanal solo borra carpetas fuente cuando el ZIP finalizado está verificado.

REGLAS FUNCIONALES QUE SE CONSERVAN
- CLOSE ALL LONGS/SHORTS es una señal anticipatoria paralela y no liquida posiciones.
- Cada ticker cierra únicamente tras confirmación de su propia reversión GEX/estructural.
- ±5% significa setup ganado y OPEN_RUNNER.
- Tocar el target GEX es un milestone, no una orden de cierre.
- Una oportunidad local contratendencia fuerte puede seguir entrando.
