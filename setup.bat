@echo off
setlocal EnableDelayedExpansion
REM Vytvori / aktualizuje prenositelne Python prostredi (.venv) ve slozce aplikace.
cd /d "%~dp0"

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "LOG=%ROOT%\data\launch.log"

if not exist "%ROOT%\data" mkdir "%ROOT%\data"
echo setup.bat started>> "%LOG%"

REM Obnov PATH (po winget instalaci bez restartu)
call "%~dp0tools\refresh-path.bat"

set "PY_CMD="
py -3.12 --version >nul 2>&1 && set "PY_CMD=py -3.12"
if not defined PY_CMD (
  py -3 --version >nul 2>&1 && set "PY_CMD=py -3"
)
if not defined PY_CMD (
  py --version >nul 2>&1 && set "PY_CMD=py"
)
if not defined PY_CMD (
  python --version >nul 2>&1 && set "PY_CMD=python"
)

if not defined PY_CMD (
  echo CHYBA: Python 3.12+ neni nainstalovan.
  echo Spust nejdriv: install.bat
  echo FAIL: python missing>> "%LOG%"
  exit /b 1
)

echo Python: %PY_CMD%>> "%LOG%"

if not exist "%VENV_PY%" (
  echo Vytvarim prenositelne prostredi .venv ...
  echo Creating venv>> "%LOG%"
  %PY_CMD% -m venv "%ROOT%\.venv" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo Nepodarilo se vytvorit .venv
    echo FAIL: venv>> "%LOG%"
    exit /b 1
  )
)

echo Instaluji Python balicky ...
"%VENV_PY%" -m pip install --upgrade pip -q >> "%LOG%" 2>&1
"%VENV_PY%" -m pip install -r "%ROOT%\requirements.txt" -q >> "%LOG%" 2>&1
if errorlevel 1 (
  echo Nepodarilo se nainstalovat zavislosti pip.
  echo FAIL pip>> "%LOG%"
  exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
  echo VAROVANI: Node.js neni v PATH - plne rozliseni YouTube muze selhat.
  echo WARN: node>> "%LOG%"
) else (
  for /f "delims=" %%N in ('node -v 2^>nul') do echo Node: %%N >> "%LOG%"
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo VAROVANI: ffmpeg neni v PATH - MP3 a slucovani stop nebude fungovat.
  echo WARN: ffmpeg>> "%LOG%"
) else (
  echo ffmpeg OK>> "%LOG%"
)

echo setup OK>> "%LOG%"
exit /b 0
