@echo off
REM Spusti aplikaci bez otevreneho CMD okna.
cd /d "%~dp0"

if not exist "data" mkdir "data"
set "LAUNCH_LOG=%~dp0data\launch.log"

echo ===== %DATE% %TIME% =====>> "%LAUNCH_LOG%"
echo start.bat>> "%LAUNCH_LOG%"

call "%~dp0tools\refresh-path.bat"

if not exist ".venv\Scripts\python.exe" (
  echo Chybi .venv — spoustim setup.bat>> "%LAUNCH_LOG%"
  call "%~dp0setup.bat" >> "%LAUNCH_LOG%" 2>&1
  if errorlevel 1 (
    echo.
    echo Aplikace neni pripravena. Spust nejdriv install.bat
    echo.
    pause
    exit /b 1
  )
) else (
  call "%~dp0setup.bat" >> "%LAUNCH_LOG%" 2>&1
)

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%~dp0.venv\Scripts\python.exe"

echo Launching>> "%LAUNCH_LOG%"
start "" "%PYW%" "%~dp0main.py"
exit /b 0
