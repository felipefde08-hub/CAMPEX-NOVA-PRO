@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto :no_venv

rem Uso: start_node.bat [pasta_de_dados]
rem Sem argumento usa storage\campex_node. Ex.: start_node.bat storage\campex_node_v2
if not "%~1"=="" set "CAMPEX_NODE_DATA_DIR=%~1"

rem O pareamento (node_id, token, URL da Cloud) vem do node_meta do proprio Node,
rem a menos que ja esteja no ambiente. O token nunca passa pelo cmd nem pela tela.
echo [CAMPEX] Iniciando CAMPEX Node com visao local. Ctrl+C para parar.
"%PY%" -c "import os, runpy; from campex_node.core.config import NodeSettings; from campex_node.storage.local_store import LocalStore; store = LocalStore(NodeSettings.from_env().database_path); store.initialize(); [os.environ.setdefault(env, value) for env, value in ((env, store.get_meta(key)) for env, key in (('CAMPEX_NODE_ID', 'node_id'), ('CAMPEX_NODE_TOKEN', 'node_token'), ('CAMPEX_NODE_CLOUD_URL', 'cloud_url'), ('CAMPEX_NODE_ORGANIZATION_ID', 'organization_id'))) if value]; os.environ.setdefault('CAMPEX_NODE_CLOUD_URL', 'http://127.0.0.1:8000/api/v1'); runpy.run_module('campex_node.main', run_name='__main__', alter_sys=True)"
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%

:no_venv
echo [CAMPEX] Python do ambiente virtual nao encontrado: .venv\Scripts\python.exe
echo [CAMPEX] Crie o .venv e instale as dependencias antes de iniciar.
endlocal & exit /b 1
