# Primera ejecución — TMAD v6.1.9

## 1. Validación

Ejecuta:

```bat
VALIDAR_TMAD_V6_1_7.bat
```

La validación debe terminar con `VALIDACION COMPLETA CORRECTA`.

## 2. Inicio

Ejecuta:

```bat
INICIAR_TMAD_DUAL_BOT.bat
```

Los encabezados deben mostrar `v6.1.9` en ambos bots.

## 3. Secuencia esperada

### Data

```text
DATA BOT · LISTO ✅
Scanner 1021/1021 · CORE 37d · GEX Daily 2d · Intraday 3d
Pendientes 0 · estado publicado a Alerts
```

### Alerts

```text
ALERTS · BUILDING CORE 🟡
Generación XX · reconstrucción/materialización en curso
```

Después:

```text
ALERTS · SHADOW READY ✅
CORE calibrado · Research habilitado · Live desactivado
```

Y fuera del horario:

```text
ALERTS · REST 🌙
Próximo inicio L–V 12:00 Madrid · solo heartbeat/coordinación
```

## 4. Lectura de Discord

### Research

```text
WATCHLIST SHADOW · PRÓXIMA SESIÓN 🌙
Entradas disponibles: 0

GEX EARLY
LVS Call · target +9,6% · falta validación/timing

CORE EARLY
LMT Call · P(+5%) 60,6% · falta target GEX
```

Nada de ese mensaje es una entrada mientras `entradas disponibles` sea `0`.

### Live

Live permanece vacío hasta que exista un estado `ENTER`. Solo recibirá:

- `BUY/SELL ALERT`
- `UPDATE POSITION`
- `UPDATE DETERIORO`
- `CIERRE DE POSICIÓN`

## 5. Comportamientos que ya no son válidos

No debe ocurrir:

- Mostrar `ERROR` y después publicar candidatos sin una transición de recuperación.
- Reconstruir CORE varias veces para la misma generación.
- Enviar `EARLY`, `READY` o `WATCH` al canal Live.
- Enviar un Fatal a Discord por intentar abrir una segunda instancia.
- Borrar el lock del proceso activo desde un segundo lanzador.

## 6. Actualización segura

El paquete no incluye `02_CONFIG`, `03_DATOS` ni `04_RUNTIME`. Tus webhooks, sesión, histórico y coordinación permanecen intactos.
