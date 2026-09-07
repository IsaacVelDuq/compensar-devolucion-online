```bat
@echo off
setlocal EnableDelayedExpansion

::==================================================
:: CONFIGURACION
::==================================================

set "NETWORK_DIR=O:\Finanzas\Tesoreria\Automatizaciones\Compensar Devoluciones Online\dist"
set "NETWORK_EXE=%NETWORK_DIR%\Compensar_devoluciones_online.exe"
set "NETWORK_CONFIG=%NETWORK_DIR%\config.json"

set "LOCAL_DIR=%LOCALAPPDATA%\Compensar_devoluciones_online"
set "LOCAL_EXE=%LOCAL_DIR%\Compensar_devoluciones_online.exe"
set "LOCAL_CONFIG=%LOCAL_DIR%\config.json"

echo.
echo ==================================================
echo   ACTUALIZADOR - Compensar Devoluciones Online
echo ==================================================
echo.

::==================================================
:: VALIDAR EJECUTABLE DE RED
::==================================================

echo [1/4] Verificando ejecutable en la red...

if not exist "%NETWORK_EXE%" (
    echo.
    echo [ADVERTENCIA] No se encontro el ejecutable de red:
    echo %NETWORK_EXE%
    echo.
    echo Se intentara utilizar la version local.
    goto CONFIGURACION
)

echo [OK] Ejecutable de red encontrado.
echo.

::==================================================
:: CREAR CARPETA LOCAL
::==================================================

echo [2/4] Verificando carpeta local...

if not exist "%LOCAL_DIR%" (
    echo Creando carpeta:
    echo %LOCAL_DIR%

    mkdir "%LOCAL_DIR%"

    if errorlevel 1 (
        echo [ERROR] No fue posible crear la carpeta local.
        pause
        exit /b 1
    )

    echo [OK] Carpeta creada.
) else (
    echo [OK] La carpeta ya existe.
)

echo.

::==================================================
:: ACTUALIZAR EJECUTABLE
::==================================================

echo [3/4] Verificando actualizacion de la aplicacion...

if not exist "%LOCAL_EXE%" (

    echo.
    echo El ejecutable local no existe.
    echo Copiando desde la red...

    copy /Y "%NETWORK_EXE%" "%LOCAL_EXE%" >nul

    if errorlevel 1 (
        echo.
        echo [ADVERTENCIA] No fue posible copiar el ejecutable.
        echo Se intentara utilizar la version local.
    ) else (
        echo [OK] Copia inicial completada.
    )

) else (

    echo.
    echo El ejecutable local ya existe.
    echo Comprobando si existe una version mas reciente...

    xcopy "%NETWORK_EXE%" "%LOCAL_EXE%" /D /Y /Q >nul

    if errorlevel 1 (
        echo [ADVERTENCIA] No fue posible actualizar el ejecutable.
        echo Se utilizara la version local.
    ) else (
        echo [OK] Aplicacion actualizada o ya estaba al dia.
    )
)

echo.

::==================================================
:: CONFIGURACION
::==================================================

:CONFIGURACION

echo [4/4] Verificando configuracion...

if exist "%LOCAL_CONFIG%" (

    echo [OK] config.json ya existe localmente.
    echo     Se conservara la configuracion actual.

) else (

    echo configuracion.json no existe localmente.
    echo Intentando copiar configuracion desde la red...

    if not exist "%NETWORK_CONFIG%" (

        echo [ADVERTENCIA] No se encontro config.json en la red.
        echo La aplicacion se ejecutara de todas formas.

    ) else (

        copy /Y "%NETWORK_CONFIG%" "%LOCAL_CONFIG%" >nul

        if errorlevel 1 (
            echo [ADVERTENCIA] No fue posible copiar config.json.
            echo La aplicacion se ejecutara de todas formas.
        ) else (
            echo [OK] config.json copiado correctamente.
        )
    )
)

echo.

::==================================================
:: VERIFICAR EJECUTABLE LOCAL
::==================================================

if not exist "%LOCAL_EXE%" (
    echo.
    echo ==================================================
    echo   ERROR
    echo ==================================================
    echo.
    echo No existe un ejecutable local para iniciar.
    echo.
    echo %LOCAL_EXE%
    echo.
    pause
    exit /b 1
)

::==================================================
:: EJECUTAR
::==================================================

echo ==================================================
echo   INICIANDO APLICACION
echo ==================================================
echo.

start "Enviar estado de cuenta" "%LOCAL_EXE%"

if errorlevel 1 (
    echo [ERROR] No se pudo iniciar la aplicacion.
    pause
    exit /b 1
)

echo [OK] Aplicacion iniciada correctamente.
echo.

:: Cerrar automaticamente
timeout /t 2 /nobreak >nul

endlocal
```
