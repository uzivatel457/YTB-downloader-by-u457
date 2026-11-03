# Builds a clean distribution ZIP for YT Downloader (no user data, no .venv).
# This script itself is NOT included in the ZIP.
# Usage:  powershell -ExecutionPolicy Bypass -File _install\make-dist.ps1

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$OutDir = $PSScriptRoot
$Staging = Join-Path $OutDir "_staging"

# Read version from main.py
$MainPy = Join-Path $Root "main.py"
$Version = "0.0.0"
if (Test-Path $MainPy) {
    $m = Select-String -Path $MainPy -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $Version = $m.Matches[0].Groups[1].Value }
}

$ZipName = "YT-Downloader-$Version.zip"
$ZipPath = Join-Path $OutDir $ZipName
$AppFolderName = "YT-Downloader"

Write-Host "Building $ZipName from $Root"

# Clean staging
if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
$AppRoot = Join-Path $Staging $AppFolderName
New-Item -ItemType Directory -Path $AppRoot | Out-Null
New-Item -ItemType Directory -Path (Join-Path $AppRoot "tools") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $AppRoot "data") | Out-Null

# Files / dirs to ship
$Files = @(
    "main.py",
    "i18n.py",
    "requirements.txt",
    "install.bat",
    "setup.bat",
    "start.bat",
    "YT Downloader.vbs",
    "README.md",
    "README.en.md",
    ".gitignore"
)

foreach ($f in $Files) {
    $src = Join-Path $Root $f
    if (Test-Path $src) {
        Copy-Item $src -Destination (Join-Path $AppRoot $f) -Force
    } else {
        Write-Warning "Missing: $f"
    }
}

$Docs = Join-Path $AppRoot "docs"
New-Item -ItemType Directory -Path $Docs | Out-Null
foreach ($shot in @("screenshot-cs.png", "screenshot-en.png")) {
    $src = Join-Path $Root "docs\$shot"
    if (Test-Path $src) {
        Copy-Item $src -Destination (Join-Path $Docs $shot) -Force
    } else {
        Write-Warning "Missing: docs\$shot"
    }
}

$Refresh = Join-Path $Root "tools\refresh-path.bat"
if (Test-Path $Refresh) {
    Copy-Item $Refresh -Destination (Join-Path $AppRoot "tools\refresh-path.bat") -Force
}

# Empty portable data marker (no config/queue/logs/cookies)
Set-Content -Path (Join-Path $AppRoot "data\.gitkeep") -Value "" -Encoding ascii

# Short install note inside package
@"
YT Downloader $Version — distribuční balíček

1. Rozbal tuto složku kamkoli
2. Spusť install.bat (Python, Node.js, ffmpeg + .venv)
3. Spusť "YT Downloader.vbs"

Uživatelská data (fronta, nastavení, logy) vzniknou ve složce data\ až po prvním spuštění.
Jazyk rozhraní se bere z Windows: čeština, slovenština, němčina, španělština, jinak angličtina.
"@ | Set-Content -Path (Join-Path $AppRoot "CTI-ME.txt") -Encoding UTF8

# Remove old zip with same name, then compress
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

# Also remove older versioned zips so _install stays clean (keep only latest)
Get-ChildItem $OutDir -Filter "YT-Downloader-*.zip" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne $ZipName } |
    Remove-Item -Force

Compress-Archive -Path $AppRoot -DestinationPath $ZipPath -Force

# Cleanup staging
Remove-Item $Staging -Recurse -Force

$size = (Get-Item $ZipPath).Length
Write-Host "OK: $ZipPath ($([math]::Round($size/1KB, 1)) KB)"
