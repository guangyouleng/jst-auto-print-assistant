@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: cannot enter the extracted application directory.
  pause
  exit /b 2
)

set "SELF_CHECK_DIR=%USERPROFILE%\.jst-auto-print\diagnostics"
if not exist "%SELF_CHECK_DIR%" mkdir "%SELF_CHECK_DIR%"
if not exist "%SELF_CHECK_DIR%" (
  echo ERROR: cannot create the private diagnostics directory.
  pause
  exit /b 3
)
set "SELF_CHECK_LOG=%SELF_CHECK_DIR%\Win10_离线自检.log"
>"%SELF_CHECK_LOG%" echo ==== JSTAutoPrint V0.5.25 Windows offline check %date% %time% ====
if errorlevel 1 (
  echo ERROR: cannot create the offline-check log in the private diagnostics directory.
  pause
  exit /b 3
)

call :check >>"%SELF_CHECK_LOG%" 2>&1
set "CHECK_RC=%ERRORLEVEL%"
type "%SELF_CHECK_LOG%"

if not "%CHECK_RC%"=="0" (
  echo.
  echo LOCAL RUNTIME CHECK FAILED. Do not start automatic printing.
  echo Diagnostic log: "%SELF_CHECK_LOG%"
  pause
  exit /b %CHECK_RC%
)

echo.
echo LOCAL RUNTIME CHECK PASSED.
echo Diagnostic log: "%SELF_CHECK_LOG%"
echo Network/time and one physical-paper canary still require manual acceptance.
pause
exit /b 0

:check
echo This check does not call the backend and does not touch any JST order.
echo Before Step 3, start the JST print component, open the dedicated browser,
echo log in to JST and open the Print Picking page.
echo.

echo Step 1/3 - checking required verification files
if not exist "%~dp0verify_jst_win10_build.ps1" (
  echo ERROR: verify_jst_win10_build.ps1 is missing. Extract the whole delivery ZIP again.
  exit /b 10
)
if not exist "%~dp0JSTAutoPrint_Win10_21H1.exe" (
  echo ERROR: JSTAutoPrint_Win10_21H1.exe is missing. Extract the whole delivery ZIP again.
  exit /b 11
)

echo Step 2/3 - running the packaged windowed EXE offline self-test
echo This step requires exit=0, version=0.5.25, tkinter=true, native_cdp=true and local_runtime=true.
echo Browser and print-service availability reported by the EXE is informational here.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_jst_win10_build.ps1" -StageDir "%~dp0" -ExecutableSelfTest
if errorlevel 1 (
  echo ERROR: packaged windowed EXE self-test failed. Browser/print ports were not checked yet.
  exit /b 20
)

echo Step 3/3 - checking dedicated browser/CDP page and local print-service ports
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_jst_win10_build.ps1" -StageDir "%~dp0" -Runtime
if errorlevel 1 (
  echo ERROR: browser/CDP or print-service readiness check failed.
  echo Verify browser port 9222 and print ports 54323/54325.
  exit /b 30
)

echo ALL OFFLINE CHECK STEPS PASSED.
exit /b 0
