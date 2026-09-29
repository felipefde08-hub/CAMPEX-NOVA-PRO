@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto :no_venv

rem Backend local no papel de "Cloud": modo serverless nao abre cameras nem
rem carrega visao (quem captura e detecta e o CAMPEX Node).
set "CAMPEX_RUNTIME=serverless"

rem Em modo serverless os padroes de banco e upload apontam para /tmp.
rem Usa o valor do ambiente ou do .env; so cai nestes se nenhum existir.
if not defined DATABASE_URL (
    findstr /b /c:"DATABASE_URL=" .env >nul 2>&1 || set "DATABASE_URL=sqlite:///./storage/campex_dev.sqlite3"
)
if not defined VIDEO_UPLOAD_DIR (
    findstr /b /c:"VIDEO_UPLOAD_DIR=" .env >nul 2>&1 || set "VIDEO_UPLOAD_DIR=./storage/video_uploads"
)
if not defined CAMPEX_BACKEND_PORT set "CAMPEX_BACKEND_PORT=8000"

echo [CAMPEX] Backend local como Cloud em http://127.0.0.1:%CAMPEX_BACKEND_PORT%/api/v1
echo [CAMPEX] Modo serverless: sem captura de camera e sem visao. Ctrl+C para parar.
"%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port %CAMPEX_BACKEND_PORT%
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%

:no_venv
echo [CAMPEX] Python do ambiente virtual nao encontrado: .venv\Scripts\python.exe
echo [CAMPEX] Crie o .venv e instale as dependencias antes de iniciar.
endlocal & exit /b 1
