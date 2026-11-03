@echo off
setlocal EnableDelayedExpansion
REM Usage: apply-update.bat "C:\path\to\YT-Downloader-x.y.z.zip" "C:\path\to\app\root"
REM Waits for the GUI to exit, replaces app files (keeps data\ and .venv\), runs setup, restarts.

set "ZIP=%~1"
set "ROOT=%~2"
if not "%ROOT:~-1%"=="\" set "ROOT=%ROOT%\"

if "%ZIP%"=="" (
  echo Missing ZIP path.
  exit /b 1
)
if "%ROOT%"=="\" (
  echo Missing app root.
  exit /b 1
)
if not exist "%ZIP%" (
  echo ZIP not found: %ZIP%
  exit /b 1
)
if not exist "%ROOT%main.py" (
  echo Invalid app root: %ROOT%
  exit /b 1
)

if not exist "%ROOT%data" mkdir "%ROOT%data"
set "LOG=%ROOT%data\update.log"
echo ===== %DATE% %TIME% =====>> "%LOG%"
echo apply-update start>> "%LOG%"
echo ZIP=%ZIP%>> "%LOG%"
echo ROOT=%ROOT%>> "%LOG%"

REM Give pythonw time to exit and release files
timeout /t 2 /nobreak >nul

set "STAGING=%TEMP%\yt-downloader-update-%RANDOM%"
if exist "%STAGING%" rmdir /s /q "%STAGING%"
mkdir "%STAGING%"

echo Expanding ZIP...>> "%LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%STAGING%' -Force"
if errorlevel 1 (
  echo Expand failed>> "%LOG%"
  rmdir /s /q "%STAGING%" 2>nul
  exit /b 1
)

set "SRC="
if exist "%STAGING%\YT-Downloader\main.py" set "SRC=%STAGING%\YT-Downloader"
if "%SRC%"=="" if exist "%STAGING%\main.py" set "SRC=%STAGING%"
if "%SRC%"=="" (
  for /d %%D in ("%STAGING%\*") do (
    if exist "%%~fD\main.py" set "SRC=%%~fD"
  )
)
if "%SRC%"=="" (
  echo Could not find app folder inside ZIP>> "%LOG%"
  rmdir /s /q "%STAGING%" 2>nul
  exit /b 1
)

echo Copying from %SRC%>> "%LOG%"
REM robocopy: /E all subdirs, /XD skip data and .venv, /NFL /NDL quiet-ish
robocopy "%SRC%" "%ROOT%" /E /XD data .venv /XF *.pyc /NFL /NDL /NJH /NJS /nc /ns /np
set "RC=%ERRORLEVEL%"
REM robocopy 0-7 = success
if %RC% GEQ 8 (
  echo robocopy failed RC=%RC%>> "%LOG%"
  rmdir /s /q "%STAGING%" 2>nul
  exit /b 1
)

echo Running setup.bat>> "%LOG%"
call "%ROOT%setup.bat" >> "%LOG%" 2>&1
set "SETUP_ERR=%ERRORLEVEL%"
if not "%SETUP_ERR%"=="0" (
  echo setup.bat failed ERR=%SETUP_ERR%>> "%LOG%"
)

rmdir /s /q "%STAGING%" 2>nul
del /f /q "%ZIP%" 2>nul

echo Restarting app>> "%LOG%"
if exist "%ROOT%YT Downloader.vbs" (
  start "" wscript //B "%ROOT%YT Downloader.vbs"
) else if exist "%ROOT%start.bat" (
  start "" "%ROOT%start.bat"
) else (
  echo No launcher found>> "%LOG%"
  exit /b 1
)

echo apply-update OK>> "%LOG%"
exit /b 0
