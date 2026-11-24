@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo Installation failed. Read the error above.
    pause
    exit /b 1
)
echo Installation completed. Run start.bat.
pause
