TMAD v6.1.24 -> v6.1.25

1) Ejecuta APLICAR_PATCH_TMAD_V6_1_25.bat sobre TMAD_Dual_Bot_OneClick_2.
2) El patch NO modifica 02_CONFIG/03_DATOS/04_RUNTIME.
3) Publica/despliega con el launcher estable PUBLICAR_Y_DESPLEGAR_TMAD.bat.
4) Para activar los 10 canales, edita DESPUÉS el archivo persistente:
   C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick\02_CONFIG\configuracion_canales.env
5) Añade ALERTS_SHADOW_WEBHOOK y LEARNING_AUDIT_WEBHOOK. Puedes renombrar/reutilizar DATA_SCHEMA como DATA_QUALITY y ALERTS_LOG como ALERTS_STATUS.
6) Reinicia ambos bots una vez editado el .env para recargar los webhooks.

Compatibilidad: si no añades aún los dos webhooks nuevos, Shadow cae a Research y Learning Audit cae a Calibration.

Robustez añadida tras revisión del 07/08:
- ticker CORE aislado/transitorio no bloqueante;
- retry técnico selectivo GEX en deep;
- manifest estricto compatible con PUBLICAR_Y_DESPLEGAR_TMAD estable.
