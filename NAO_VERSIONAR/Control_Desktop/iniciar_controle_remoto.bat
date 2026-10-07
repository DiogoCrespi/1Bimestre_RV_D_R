@echo off
title Servidor de Controle Remoto - Antigravity
cd /d "%~dp0"

echo ===================================================
echo   Iniciando Servidor de Controle Remoto...
echo   Acesse no navegador: http://127.0.0.1:7842
echo ===================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    if exist "C:\Python314\python.exe" (
        set "PY_CMD=C:\Python314\python.exe"
    ) else (
        echo [ERRO] Python nao foi encontrado no PATH do sistema.
        pause
        exit /b 1
    )
) else (
    set "PY_CMD=python"
)

start "" http://127.0.0.1:7842
"%PY_CMD%" remote_control_server.py
pause
