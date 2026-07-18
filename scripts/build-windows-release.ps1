param(
  [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# ─────────────────────────────────────────────────────────────
# 1. Python sidecar (mole.py) → dist/wmole.exe (PyInstaller)
#    Bu exe hem standalone CLI asset'i hem de Tauri sidecar kaynağı.
# ─────────────────────────────────────────────────────────────
Write-Host "==> Building sidecar (mole.py) with PyInstaller..." -ForegroundColor Cyan
$sidecarOutput = Join-Path $repoRoot "dist/wmole.exe"
Remove-Item -LiteralPath $sidecarOutput -Force -ErrorAction SilentlyContinue
py -3 -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --name wmole `
  --icon assets/icons/desktop/wmole.ico `
  --exclude-module IPython `
  --exclude-module matplotlib `
  --exclude-module numpy `
  --exclude-module PIL `
  --exclude-module PyQt5 `
  --exclude-module PyQt6 `
  --exclude-module PySide6 `
  --exclude-module kivy `
  --exclude-module kivymd `
  mole.py

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed ($LASTEXITCODE)"
}
if (-not (Test-Path -LiteralPath $sidecarOutput)) {
  throw "dist/wmole.exe was not created."
}

# ─────────────────────────────────────────────────────────────
# 2. Sidecar'ı Tauri'nin beklediği hedef-üçlü adıyla kopyala.
# ─────────────────────────────────────────────────────────────
$sidecarDir = Join-Path $repoRoot "gui/src-tauri/binaries"
New-Item -ItemType Directory -Force -Path $sidecarDir | Out-Null
$sidecar = Join-Path $sidecarDir "wmole-x86_64-pc-windows-msvc.exe"
Copy-Item -LiteralPath $sidecarOutput -Destination $sidecar -Force
Write-Host "    sidecar -> $sidecar"

# ─────────────────────────────────────────────────────────────
# 3. Tauri GUI'yi derle → NSIS installer üret.
# ─────────────────────────────────────────────────────────────
Write-Host "==> Building Tauri GUI (npm run tauri build)..." -ForegroundColor Cyan
Push-Location (Join-Path $repoRoot "gui")
try {
  npm run tauri build
  if ($LASTEXITCODE -ne 0) { throw "tauri build failed ($LASTEXITCODE)" }
} finally {
  Pop-Location
}

# ─────────────────────────────────────────────────────────────
# 4. NSIS çıktısını release/wmole-$Version-setup.exe olarak kopyala.
#    (in-app updater bu ada göre asset arıyor: "-setup.exe")
# ─────────────────────────────────────────────────────────────
$nsisDir = Join-Path $repoRoot "gui/src-tauri/target/release/bundle/nsis"
$built = Get-ChildItem -Path $nsisDir -Filter "*-setup.exe" -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $built) {
  throw "NSIS installer not found in $nsisDir"
}

New-Item -ItemType Directory -Force -Path (Join-Path $repoRoot "release") | Out-Null
$installer = Join-Path $repoRoot "release/wmole-$Version-setup.exe"
Copy-Item $built.FullName $installer -Force

Write-Host "==> Done:" -ForegroundColor Green
Write-Host "    GUI installer (NSIS): $installer"
Write-Host "    CLI exe (standalone): $repoRoot\dist\wmole.exe"
