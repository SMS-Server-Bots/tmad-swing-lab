# TMAD History Query Worker

Servicio auxiliar del propio TMAD para consultar el historico frio de Cloudflare R2 sin cargar bases grandes en Supabase Edge.

## Arquitectura

Este componente vive en el mismo repositorio y versionado de TMAD, pero corre como proceso independiente del loop Data/Alerts.

R2 sigue siendo el master. El VPS descarga bajo demanda un ZIP verificado, valida tamano + SHA256 contra `manifests/data_bot_archive/latest.json`, lo cachea en `04_RUNTIME/history_query_cache` y consulta localmente.

## Capacidades

- listar cualquier ZIP verificado;
- leer cabeceras/texto;
- abrir SQLite grandes con sqlite3 nativo;
- ejecutar consultas SELECT/WITH/EXPLAIN en read-only;
- consultar CSV grandes en streaming con filtros;
- cache TTL + limite de tamano;
- credenciales R2 solo lectura;
- sin operaciones de escritura/borrado en R2.

## Endpoints

Todos salvo `/health` requieren `x-tmad-history-token`.

- `GET /health`
- `GET /v1/catalog/summary`
- `POST /v1/list`
- `POST /v1/text/head`
- `POST /v1/sqlite/schema`
- `POST /v1/sqlite/query`
- `POST /v1/csv/query`
- `GET /v1/cache/status`
- `POST /v1/cache/prune`

## Configuracion

Carga los ficheros locales `02_CONFIG/tmad_history_query.env`, `02_CONFIG/tmad_cloud.secret.env` y `02_CONFIG/tmad_history_query.secret.env`. No se guarda ningun secreto en GitHub.

Por defecto escucha solo en `127.0.0.1:8787`. No se debe abrir ese puerto directamente a Internet. Para el acceso de ChatGPT/Supabase se publicara mediante Cloudflare Tunnel con HTTPS.
