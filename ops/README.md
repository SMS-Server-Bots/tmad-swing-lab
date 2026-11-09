# Operaciones de datos TMAD en el VPS

## Diagnóstico de la situación actual (09/11/2026)

| Componente | Estado | Causa probable |
|---|---|---|
| ZIP inmutables → GitHub Releases | ✅ Al día (índice del 08/11) | — |
| Snapshot de las `.db` vivas → `tmad-data-latest` | ❌ Parado desde el 27/09 | `TMAD_DATA_SNAPSHOT.py` copia **todas** las `.db` completas a `%TEMP%` (Alerts: 11,7 GB + WAL) y luego crea el 7z en la misma carpeta; con 10,7 GB libres no cabe |
| Cloudflare R2 + catálogo en Supabase | ❌ Parado desde el 22/09 | La subida a R2 no está en el código de los repositorios; probablemente fue un proceso manual o externo |
| Disco del VPS | ⚠️ 10,7 % libre | La base de Alerts crece sin retención y el WAL va por 5,6 GB |

## Recomendación: no migrar las bases a Supabase

- Los bots están construidos sobre SQLite local, y es lo correcto para un único escritor en el VPS. Pasar a Postgres en Supabase supondría reescribir la capa de datos de los dos bots y añadir latencia de red en cada escritura. Además, 11,7 GB y subiendo superan el almacenamiento incluido en sus planes básicos.
- Cloudflare R2 sí encaja como **segunda copia de objetos**: sin coste por descarga y unos 0,015 $/GB-mes. Pero hoy GitHub Releases ya cumple esa función gratis y con verificación SHA. R2 solo aporta si se quiere una copia redundante fuera de GitHub.
- **Lo urgente es que exista un backup remoto de la base de Alerts** y que el disco tenga margen.

## Pasos (en este orden)

### 1. Diagnóstico de solo lectura (2 minutos)

```powershell
cd <ruta de este repo>\ops
powershell -ExecutionPolicy Bypass -File .\DIAGNOSTICO_DATOS_VPS.ps1 -ProductionPath "C:\...\TMAD"
```

Genera `diagnostico_datos_<fecha>.txt` con el espacio por disco, el tamaño de cada carpeta y de cada base/WAL, los ZIP locales, el estado de las tareas programadas y el log del backup. Pega el resultado en el chat. Con eso confirmo la causa y dimensiono el resto.

### 2. Backup remoto de la base de Alerts sin copia completa

```powershell
# prueba local sin subir (comprueba espacio, tiempo y tamaño comprimido)
powershell -ExecutionPolicy Bypass -File .\BACKUP_ALERTS_DB_STREAM.ps1 -ProductionPath "C:\...\TMAD" -NoUpload
# real: sube a la release tmad-alerts-db-latest y verifica en remoto
powershell -ExecutionPolicy Bypass -File .\BACKUP_ALERTS_DB_STREAM.ps1 -ProductionPath "C:\...\TMAD"
```

- Lee la base en solo lectura, dentro de una transacción de lectura: obtiene una instantánea coherente aunque el bot siga escribiendo.
- Escribe solo el volcado comprimido por partes de 1,8 GB, normalmente entre el 10 y el 20 % del tamaño de la base. Si `-WorkDir` está en otro disco con más espacio, mejor.
- Sube el juego nuevo, verifica su SHA en remoto y solo entonces retira el anterior.
- Mientras dura el volcado, el WAL no se puede truncar. Conviene programarlo a las 04:00, cuando el bot está tranquilo, igual que el backup mutable actual.
- Restaurar: `python tmad_sqlite_stream_backup.py restore <carpeta_con_partes> nueva.db`. Verifica los SHA y los recuentos de filas de cada tabla.
- **Probado** con la base antigua del Scanner (3,9 GB): volcado de 326 MB (8 %) en 146 s y restauración en 126 s. Recuentos, sumas de control de las tablas grandes y esquema idénticos. Para la base de Alerts se esperan ~1–2 GB y entre 10 y 20 minutos.

### 3. Espacio en disco

Con los datos del diagnóstico decidiremos entre: ampliar el disco del VPS (lo más sencillo), mover `03_DATOS\alerts_bot\db\archive` a otro volumen, o aplicar retención a las tablas de Alerts que ya están archivadas en las capas diarias.

### 4. Protección del histórico (6.2.68)

Sigue pendiente en `tmad-dual-bot`: `rebuild_canonical_daily_history` en `main` aún hace un `DELETE` global.
