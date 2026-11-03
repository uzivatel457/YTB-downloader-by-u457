@echo off
REM Doplni do PATH cesty po winget instalaci (bez restartu PC).
set "Path=%ProgramFiles%\nodejs;%ProgramFiles(x86)%\nodejs;%Path%"

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
  set "Path=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%Path%"
)
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
  set "Path=%LocalAppData%\Programs\Python\Python313;%LocalAppData%\Programs\Python\Python313\Scripts;%Path%"
)
if exist "C:\Python312\python.exe" set "Path=C:\Python312;C:\Python312\Scripts;%Path%"
if exist "C:\Python313\python.exe" set "Path=C:\Python313;C:\Python313\Scripts;%Path%"

REM ffmpeg (Gyan winget) — typicka cesta
if exist "%LocalAppData%\Microsoft\WinGet\Packages" (
  for /d %%D in ("%LocalAppData%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*") do (
    if exist "%%D\ffmpeg-*\bin\ffmpeg.exe" (
      for /d %%B in ("%%D\ffmpeg-*") do set "Path=%%B\bin;%Path%"
    )
  )
)

exit /b 0
