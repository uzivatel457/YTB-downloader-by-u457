@echo off
setlocal EnableDelayedExpansion
title YT Downloader — instalace zavislosti
cd /d "%~dp0"

echo ============================================
echo   YT Downloader — instalace zavislosti
echo ============================================
echo.
echo Nainstaluje (pokud chybi):
echo   - Python 3.12
echo   - Node.js LTS
echo   - ffmpeg
echo   - Python balicky do slozky .venv (prenositelne)
echo.

set "ROOT=%~dp0"
if not exist "%ROOT%data" mkdir "%ROOT%data"
set "LOG=%ROOT%data\install.log"

echo ===== %DATE% %TIME% =====>> "%LOG%"
echo install.bat start>> "%LOG%"

where winget >nul 2>&1
if errorlevel 1 (
  echo CHYBA: winget neni dostupny.
  echo Nainstaluj rucne:
  echo   https://www.python.org/downloads/
  echo   https://nodejs.org/
  echo   https://www.gyan.dev/ffmpeg/builds/
  echo FAIL: no winget>> "%LOG%"
  pause
  exit /b 1
)

set "NEED_RESTART=0"

call :EnsurePackage "Python 3.12" "Python.Python.3.12" "py -3.12 --version"
call :EnsurePackage "Node.js LTS" "OpenJS.NodeJS.LTS" "node -v"
call :EnsurePackage "ffmpeg" "Gyan.FFmpeg" "ffmpeg -version"

call "%~dp0tools\refresh-path.bat"

echo.
echo Nastavuji prenositelne Python prostredi ...
call "%~dp0setup.bat"
set "SETUP_ERR=%ERRORLEVEL%"
if not "%SETUP_ERR%"=="0" (
  echo.
  echo Instalace systemovych balicku probehla, ale setup .venv selhal.
  echo Zkus restartovat PC a spustit install.bat znovu.
  pause
  exit /b 1
)

echo.
echo ============================================
echo   Instalace dokoncena!
echo ============================================
echo.
echo Spust aplikaci:
echo   - dvojklik na "YT Downloader.vbs"
echo   - nebo start.bat
echo.
echo Slozka aplikace je prenositelna — muzes ji zkopirovat
echo na USB / jiny disk (slozka .venv musi byt soucasti).
echo.
if "%NEED_RESTART%"=="1" (
  echo POZNAMKA: Pokud Python/Node/ffmpeg nefunguji,
  echo restartuj pocitac nebo odhlas prihlaseni.
  echo.
)
pause
exit /b 0

:EnsurePackage
set "LABEL=%~1"
set "WINGET_ID=%~2"
set "CHECK_CMD=%~3"
echo.
echo --- %LABEL% ---
call %CHECK_CMD% >nul 2>&1
if not errorlevel 1 (
  echo   [OK] %LABEL% uz je nainstalovan.
  echo OK %LABEL%>> "%LOG%"
  exit /b 0
)

echo   Instaluji %LABEL% pres winget ...
winget install %WINGET_ID% --accept-package-agreements --accept-source-agreements --disable-interactivity >> "%LOG%" 2>&1
if errorlevel 1 (
  echo   VAROVANI: winget instalace %LABEL% selhala nebo byla prerusena.
  echo   Zkus: winget install %WINGET_ID%
  echo WARN winget %WINGET_ID%>> "%LOG%"
  set "NEED_RESTART=1"
) else (
  echo   [OK] %LABEL% nainstalovan.
  echo INSTALLED %LABEL%>> "%LOG%"
  set "NEED_RESTART=1"
)
call "%~dp0tools\refresh-path.bat"
exit /b 0
