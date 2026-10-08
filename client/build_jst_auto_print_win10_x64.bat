@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: cannot enter the extracted source directory.
  pause
  exit /b 2
)

set "BUILD_LOG=%~dp0build_win10_x64.log"
set "BUILD_VENV=%~dp0.build-venv-win10"
set "WORK_DIR=%~dp0.pyinstaller-work-win10"
set "SPEC_DIR=%~dp0.pyinstaller-spec-win10"
set "DIST_ROOT=%~dp0dist_win10_x64"
set "STAGE_DIR=%DIST_ROOT%\JSTAutoPrint_Win10_21H1"
set "FINAL_ZIP=%DIST_ROOT%\JSTAutoPrint_Win10_21H1_V0.5.25.zip"
set "TEMP_ZIP=%DIST_ROOT%\JSTAutoPrint_Win10_21H1_V0.5.25.building.zip"
set "ZIP_SHA256=%FINAL_ZIP%.sha256"

>"%BUILD_LOG%" echo ==== JSTAutoPrint V0.5.25 Windows x64 build %date% %time% ====
call :build >>"%BUILD_LOG%" 2>&1
set "BUILD_RC=%ERRORLEVEL%"
type "%BUILD_LOG%"

if not "%BUILD_RC%"=="0" (
  echo.
  echo BUILD FAILED. No verified delivery ZIP was produced.
  echo Diagnostic log: "%BUILD_LOG%"
  pause
  exit /b %BUILD_RC%
)

echo.
echo BUILD AND ZIP VERIFICATION PASSED.
echo Delivery file: "%FINAL_ZIP%"
echo Diagnostic log: "%BUILD_LOG%"
echo Copy the whole ZIP to Win10 and extract all files. Never copy only the EXE.
pause
exit /b 0

:build
echo Compatibility artifact name: Win10_21H1, Intel/AMD x64
echo Production target: a 64-bit Windows release still receiving Microsoft security updates
echo Step 1/10 - validating source inputs
for %%F in (
  "jst_auto_print_app.py"
  "jst_operator_config.json"
  "jst_auto_print_requirements_win10_x64.txt"
  "build_jst_artifacts.ps1"
  "Win10_21H1_试运行不打印.bat"
  "Win10_离线自检.bat"
  "Win10_后台网络诊断.bat"
  "verify_jst_win10_build.ps1"
  "聚水潭安全打单助手_使用说明.md"
  "聚水潭安全打单助手_V0.5.25_运行逻辑与流程图.md"
) do (
  if not exist "%%~F" (
    echo ERROR: required input is missing: %%~F
    exit /b 10
  )
)

echo Step 2/10 - checking Python 3.10 AMD64
where py >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python launcher py.exe was not found.
  exit /b 11
)
py -3.10-64 -c "import platform,struct,sys; assert sys.version_info[:2]==(3,10),sys.version; assert struct.calcsize('P')*8==64; assert platform.machine().upper() in ('AMD64','X86_64'),platform.machine(); print(sys.version,platform.machine())"
if errorlevel 1 (
  echo ERROR: Python 3.10 AMD64/x64 is required. ARM64 and 32-bit Python are not accepted.
  exit /b 12
)

echo Step 3/10 - validating V0.5.25/schema source, self-test CLI and deployment configuration
py -3.10-64 -c "import ast,json,pathlib,re; p=pathlib.Path('jst_auto_print_app.py'); source=p.read_text(encoding='utf-8'); tree=ast.parse(source); vals={t.id:n.value.value for n in tree.body if isinstance(n,ast.Assign) and isinstance(n.value,ast.Constant) for t in n.targets if isinstance(t,ast.Name)}; assert vals.get('APP_VERSION')=='0.5.25',vals.get('APP_VERSION'); assert vals.get('API_SCHEMA_VERSION')==5,vals.get('API_SCHEMA_VERSION'); assert vals.get('PLANNER_SCHEMA_VERSION')==5,vals.get('PLANNER_SCHEMA_VERSION'); assert '--self-test' in source and '--self-test-output' in source,'self-test CLI is incomplete'; c=json.loads(pathlib.Path('jst_operator_config.json').read_text(encoding='utf-8')); assert c.get('backend_mode')=='local' or re.fullmatch(r'https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_./-]+',str(c.get('api_url',''))); assert c.get('backend_mode')=='local' or re.fullmatch(r'[A-Za-z0-9_-]{32,128}',str(c.get('api_token',''))); assert 1024<=int(c.get('debug_port',0))<=65535; assert int(c.get('loop_seconds',0))==5,c.get('loop_seconds'); print('source version/schema, self-test CLI and deployment config: OK')"
if errorlevel 1 (
  echo ERROR: source version, self-test CLI or deployment configuration validation failed.
  exit /b 13
)

echo Step 4/10 - removing stale build output
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_jst_artifacts.ps1" -Operation Clean -SourceRoot "%~dp0."
if errorlevel 1 (
  echo ERROR: stale build output could not be removed. Close Explorer, antivirus scans and old build processes.
  exit /b 14
)
if not exist "%DIST_ROOT%" mkdir "%DIST_ROOT%"
if not exist "%DIST_ROOT%" (
  echo ERROR: dist_win10_x64 could not be created.
  exit /b 15
)

echo Step 5/10 - creating clean build environment
py -3.10-64 -m venv "%BUILD_VENV%"
if errorlevel 1 (
  echo ERROR: virtual environment creation failed.
  exit /b 20
)
if not exist "%BUILD_VENV%\Scripts\python.exe" (
  echo ERROR: virtual environment is incomplete; python.exe is missing.
  exit /b 21
)
"%BUILD_VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --require-hashes -r "jst_auto_print_requirements_win10_x64.txt"
if errorlevel 1 (
  echo ERROR: hash-locked dependency installation failed. Do not bypass --require-hashes.
  exit /b 23
)
"%BUILD_VENV%\Scripts\python.exe" -c "import importlib.metadata as m,re,struct,websocket; expected={'pip':'26.2','setuptools':'84.0.0','altgraph':'0.17.5','packaging':'26.3','pefile':'2023.2.7','pyinstaller-hooks-contrib':'2026.7','pywin32-ctypes':'0.2.3','pyinstaller':'6.22.2','websocket-client':'1.9.0'}; norm=lambda value:re.sub(r'[-_.]+','-',value).lower(); actual={norm(d.metadata['Name']):d.version for d in m.distributions() if d.metadata['Name']}; assert actual==expected,actual; assert struct.calcsize('P')*8==64; print('hash-locked build environment: OK',actual)"
if errorlevel 1 (
  echo ERROR: installed build dependencies are incomplete.
  exit /b 24
)

echo Step 6/10 - building PyInstaller onedir payload
"%BUILD_VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --noupx ^
  --name "JSTAutoPrint_Win10_21H1" ^
  --contents-directory "." ^
  --collect-all websocket ^
  --add-data "%~dp0jst_operator_config.json;." ^
  --distpath "%DIST_ROOT%" ^
  --workpath "%WORK_DIR%" ^
  --specpath "%SPEC_DIR%" ^
  "jst_auto_print_app.py"
if errorlevel 1 (
  echo ERROR: PyInstaller failed.
  exit /b 30
)

echo Step 7/10 - adding operator and verification files
copy /y "Win10_21H1_试运行不打印.bat" "%STAGE_DIR%\试运行不打印.bat" >nul
if errorlevel 1 exit /b 31
copy /y "Win10_离线自检.bat" "%STAGE_DIR%\Win10_离线自检.bat" >nul
if errorlevel 1 exit /b 32
copy /y "Win10_后台网络诊断.bat" "%STAGE_DIR%\Win10_后台网络诊断.bat" >nul
if errorlevel 1 exit /b 33
copy /y "verify_jst_win10_build.ps1" "%STAGE_DIR%\verify_jst_win10_build.ps1" >nul
if errorlevel 1 exit /b 34
copy /y "聚水潭安全打单助手_使用说明.md" "%STAGE_DIR%\使用说明_V0.5.25.md" >nul
if errorlevel 1 exit /b 35
copy /y "聚水潭安全打单助手_V0.5.25_运行逻辑与流程图.md" "%STAGE_DIR%\运行逻辑与流程图_V0.5.25.md" >nul
if errorlevel 1 exit /b 36
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_jst_artifacts.ps1" -Operation WriteManifest -SourceRoot "%~dp0."
if errorlevel 1 exit /b 37

echo Step 8/10 - running staged windowed EXE self-test and validating staged payload
echo Build gate note: browser/CDP and print-service ports are not required in this step.
powershell -NoProfile -ExecutionPolicy Bypass -File "verify_jst_win10_build.ps1" -StageDir "%STAGE_DIR%" -ExecutableSelfTest
if errorlevel 1 (
  echo ERROR: staged payload or windowed EXE self-test failed.
  exit /b 40
)

echo Step 9/10 - creating and validating delivery ZIP
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_jst_artifacts.ps1" -Operation CreateZip -SourceRoot "%~dp0."
if errorlevel 1 (
  echo ERROR: ZIP creation failed.
  exit /b 41
)
powershell -NoProfile -ExecutionPolicy Bypass -File "verify_jst_win10_build.ps1" -StageDir "%STAGE_DIR%" -ZipPath "%TEMP_ZIP%"
if errorlevel 1 (
  echo ERROR: ZIP content verification failed. The temporary ZIP will not be delivered.
  exit /b 42
)

echo Step 10/10 - publishing verified ZIP and verifying it again
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_jst_artifacts.ps1" -Operation Publish -SourceRoot "%~dp0."
if errorlevel 1 (
  echo ERROR: verified ZIP could not be published.
  exit /b 43
)
powershell -NoProfile -ExecutionPolicy Bypass -File "verify_jst_win10_build.ps1" -StageDir "%STAGE_DIR%" -ZipPath "%FINAL_ZIP%"
if errorlevel 1 (
  echo ERROR: final delivery ZIP verification failed.
  exit /b 44
)
if not exist "%FINAL_ZIP%" (
  echo ERROR: final delivery ZIP is missing after verification.
  exit /b 45
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_jst_artifacts.ps1" -Operation WriteZipDigest -SourceRoot "%~dp0."
if errorlevel 1 (
  echo ERROR: final ZIP SHA256 file could not be written.
  exit /b 46
)
echo VERIFIED DELIVERY ZIP: %FINAL_ZIP%
echo VERIFY THIS DIGEST THROUGH A SEPARATE TRUSTED CHANNEL: %ZIP_SHA256%
exit /b 0
