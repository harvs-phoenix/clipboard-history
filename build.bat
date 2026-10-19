@echo off
REM Build a single self-contained clipboard_history.exe
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (set "PY=python") else (set "PY=py")

echo Installing dependencies...
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo.
echo Building clipboard_history.exe...
%PY% -m PyInstaller --onefile --noconsole --add-data "config.json;." --name clipboard_history clipboard_app.py --noconfirm
if errorlevel 1 goto :fail

if not exist dist\clipboard_history.exe goto :fail

echo.
echo Done: %~dp0dist\clipboard_history.exe
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
