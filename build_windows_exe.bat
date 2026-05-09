@echo off
setlocal

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found. Install Python 3.10+ first.
  exit /b 1
)

py -m pip install --upgrade pyinstaller
if errorlevel 1 exit /b 1

py -m PyInstaller --onefile --name recover-files recover_files.py
if errorlevel 1 exit /b 1

echo.
echo Built dist\recover-files.exe
echo Run Command Prompt or PowerShell as Administrator when carving raw disks.
