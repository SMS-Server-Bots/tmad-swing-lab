# TMAD · Despliegue desde GitHub

Copiar estos archivos en:

`C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2`

## Desplegar una release explícita

```powershell
Set-Location "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2"
.\DESPLEGAR_RELEASE_TMAD.ps1 -Tag v6.1.9
```

El script:

1. Actualiza y verifica el tag contra GitHub.
2. Genera un paquete exacto desde ese tag.
3. Comprueba que no incluya `.env`, `02_CONFIG`, `03_DATOS` ni `04_RUNTIME`.
4. Ejecuta el validador fuera de producción.
5. Crea un backup de los programas actuales.
6. Detiene Data y Alerts.
7. Sustituye únicamente `01_PROGRAMAS` y los lanzadores permitidos.
8. Vuelve a validar en producción.
9. Arranca los bots y comprueba sus locks.
10. Si falla después del intercambio, restaura automáticamente la versión anterior.

Para desplegar sin arrancar los bots:

```powershell
.\DESPLEGAR_RELEASE_TMAD.ps1 -Tag v6.1.9 -NoStart
```

También se puede utilizar el wrapper BAT:

```cmd
DESPLEGAR_RELEASE_TMAD.bat -Tag v6.1.9
```

## Rollback manual

Restaura el último backup registrado:

```powershell
.\ROLLBACK_TMAD.ps1
```

Restaura un backup concreto:

```powershell
.\ROLLBACK_TMAD.ps1 -BackupPath "C:\Users\Administrator\Desktop\TMAD_DEPLOY_BACKUPS\20260730_020000_v6.1.9"
```

## Rutas locales

- Backups: `C:\Users\Administrator\Desktop\TMAD_DEPLOY_BACKUPS`
- Logs: `TMAD_Dual_Bot_OneClick\04_RUNTIME\deployments`
- Último backup: `04_RUNTIME\deployments\last_backup.txt`

## Contenido que nunca se sustituye

- `.env`
- `02_CONFIG`
- `03_DATOS`
- `04_RUNTIME`


## Compatibilidad Windows v2

La v2 no renombra ni elimina la carpeta raíz `01_PROGRAMAS`. Usa `robocopy /MIR` con reintentos, por lo que tolera mejor ventanas del Explorador, antivirus y handles transitorios. El rollback usa el mismo método.
