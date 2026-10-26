@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: cannot enter the extracted source directory.
  pause
  exit /b 2
)

set "RUNTIME_VENV=%~dp0.runtime-venv-win10"
set "RUNTIME_PYTHON=%RUNTIME_VENV%\Scripts\python.exe"
set "DIAG_LOG=%~dp0Win10_源码启动诊断.log"

>>"%DIAG_LOG%" echo.
>>"%DIAG_LOG%" echo ==== JSTAutoPrint V0.5.25 source start %date% %time% ====
call :prepare_runtime >>"%DIAG_LOG%" 2>&1
set "PREPARE_RC=%ERRORLEVEL%"
if not "%PREPARE_RC%"=="0" (
  echo SOURCE RUNTIME PREPARATION FAILED.
  echo Diagnostic log: "%DIAG_LOG%"
  type "%DIAG_LOG%"
  pause
  exit /b %PREPARE_RC%
)

echo Starting V0.5.25 in no-print trial mode.
echo IMPORTANT: after clicking Start, the trial can still change the carrier and obtain a real waybill.
echo Diagnostic log: "%DIAG_LOG%"
>>"%DIAG_LOG%" echo Launching application in --no-print mode
"%RUNTIME_PYTHON%" "%~dp0jst_auto_print_app.py" --no-print >>"%DIAG_LOG%" 2>&1
set "APP_RC=%ERRORLEVEL%"
>>"%DIAG_LOG%" echo Application exit code: %APP_RC% at %date% %time%
if not "%APP_RC%"=="0" (
  echo.
  echo APPLICATION EXITED WITH ERROR %APP_RC%.
  echo Diagnostic log: "%DIAG_LOG%"
  type "%DIAG_LOG%"
  pause
  exit /b %APP_RC%
)
echo Application closed normally. Diagnostic log: "%DIAG_LOG%"
exit /b 0

:prepare_runtime
for %%F in ("jst_auto_print_app.py" "jst_local_coordinator.py" "jst_local_source.py" "jst_local_store.py" "jst_openapi.py" "jst_print_shadow_plan.py" "jst_operator_config.json" "jst_auto_print_runtime_requirements_win10_x64.txt" "build_jst_artifacts.ps1") do (
  if not exist "%%~F" (
    echo ERROR: required source file is missing: %%~F
    exit /b 10
  )
)

where py >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python launcher py.exe was not found.
  exit /b 11
)
py -3.10-64 -c "import platform,struct,sys; assert sys.version_info[:2]==(3,10); assert struct.calcsize('P')*8==64; assert platform.machine().upper() in ('AMD64','X86_64'); print(sys.version,platform.machine())"
if errorlevel 1 (
  echo ERROR: source fallback requires Python 3.10 AMD64/x64.
  exit /b 12
)

set "REBUILD_VENV=0"
if not exist "%RUNTIME_PYTHON%" set "REBUILD_VENV=1"
if exist "%RUNTIME_PYTHON%" (
  "%RUNTIME_PYTHON%" -c "import struct,sys; assert sys.version_info[:2]==(3,10); assert struct.calcsize('P')*8==64" >nul 2>&1
  if errorlevel 1 set "REBUILD_VENV=1"
)
if exist "%RUNTIME_PYTHON%" if "%REBUILD_VENV%"=="0" (
  "%RUNTIME_PYTHON%" -c "import importlib.metadata as m,re,websocket; expected={'pip':'26.2','setuptools':'84.0.0','websocket-client':'1.9.0'}; norm=lambda value:re.sub(r'[-_.]+','-',value).lower(); actual={norm(d.metadata['Name']):d.version for d in m.distributions() if d.metadata['Name']}; assert actual==expected,actual" >nul 2>&1
  if errorlevel 1 set "REBUILD_VENV=1"
)
if "%REBUILD_VENV%"=="1" (
  echo Existing runtime is missing or incomplete; rebuilding it.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_jst_artifacts.ps1" -Operation CleanRuntime -SourceRoot "%~dp0."
  if errorlevel 1 (
    echo ERROR: incomplete runtime could not be removed. Close Python processes and retry.
    exit /b 20
  )
  py -3.10-64 -m venv "%RUNTIME_VENV%"
  if errorlevel 1 (
    echo ERROR: runtime virtual environment creation failed.
    exit /b 21
  )
  if not exist "%RUNTIME_PYTHON%" (
    echo ERROR: runtime virtual environment is incomplete after creation.
    exit /b 22
  )
)

"%RUNTIME_PYTHON%" -c "import importlib.metadata as m,re,websocket; expected={'pip':'26.2','setuptools':'84.0.0','websocket-client':'1.9.0'}; norm=lambda value:re.sub(r'[-_.]+','-',value).lower(); actual={norm(d.metadata['Name']):d.version for d in m.distributions() if d.metadata['Name']}; assert actual==expected,actual" >nul 2>&1
if errorlevel 1 (
  echo Installing the complete hash-locked native CDP runtime.
  "%RUNTIME_PYTHON%" -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --require-hashes -r "%~dp0jst_auto_print_runtime_requirements_win10_x64.txt"
  if errorlevel 1 (
    echo ERROR: hash-locked runtime installation failed. Never bypass --require-hashes.
    exit /b 24
  )
)

"%RUNTIME_PYTHON%" -c "import ast,importlib.metadata as m,json,pathlib,re,struct,websocket; tree=ast.parse(pathlib.Path('jst_auto_print_app.py').read_text(encoding='utf-8')); vals={t.id:n.value.value for n in tree.body if isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant) for t in n.targets if isinstance(t,ast.Name)}; assert vals.get('APP_VERSION')=='0.5.25',vals.get('APP_VERSION'); assert vals.get('API_SCHEMA_VERSION')==5,vals.get('API_SCHEMA_VERSION'); assert vals.get('PLANNER_SCHEMA_VERSION')==5,vals.get('PLANNER_SCHEMA_VERSION'); expected={'pip':'26.2','setuptools':'84.0.0','websocket-client':'1.9.0'}; norm=lambda value:re.sub(r'[-_.]+','-',value).lower(); actual={norm(d.metadata['Name']):d.version for d in m.distributions() if d.metadata['Name']}; assert actual==expected,actual; c=json.loads(pathlib.Path('jst_operator_config.json').read_text(encoding='utf-8')); mode=c.get('backend_mode','auto'); assert mode in ('auto','local','remote'),'invalid backend_mode'; local=mode=='local' or (mode=='auto' and not c.get('api_url') and not c.get('api_token')); assert local or re.fullmatch(r'https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_./-]+',str(c.get('api_url',''))),'invalid API URL'; assert local or re.fullmatch(r'[A-Za-z0-9_-]{32,128}',str(c.get('api_token',''))),'invalid API token'; assert 1024<=int(c.get('debug_port',0))<=65535; assert int(c.get('loop_seconds',0))==5,c.get('loop_seconds'); assert struct.calcsize('P')*8==64; print('source version/schema, hash-locked config and native CDP runtime: OK')"
if errorlevel 1 (
  echo ERROR: source, deployment config or native CDP validation failed.
  exit /b 25
)
exit /b 0
