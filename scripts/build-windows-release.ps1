param(
  [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "Building wmole.exe with PyInstaller..."
py -3 -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --name wmole `
  --icon assets/icons/desktop/wmole.ico `
  mole.py

if (-not (Test-Path "dist/wmole.exe")) {
  throw "dist/wmole.exe was not created."
}

$isccCandidates = @(
  (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
  (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  throw "ISCC.exe not found. Checked: $($isccCandidates -join ', ')"
}

Write-Host "Compiling installer..."
& $iscc "/DMyAppVersion=$Version" "installer/wmole.iss"

$installer = "release/wmole-$Version-setup.exe"
if (-not (Test-Path $installer)) {
  throw "Installer not found: $installer"
}

Write-Host "Done:"
Write-Host "  EXE:      $repoRoot\dist\wmole.exe"
Write-Host "  Installer $repoRoot\$installer"
