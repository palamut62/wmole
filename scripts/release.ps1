param(
  [Parameter(Mandatory=$true)][string]$Version,
  [string]$Notes = "",
  [string]$NotesFile = "",
  [switch]$Prerelease,
  [switch]$SkipPush,
  [switch]$SkipBuild
)

# wmole one-shot release pipeline:
#   1. bump installer version
#   2. git add/commit/push
#   3. build wmole.exe + Inno Setup installer
#   4. gh release create with assets
# Changelog + web download button update themselves at runtime
# via the GitHub Releases API (see index.html: loadReleases).

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$tag = "v$Version"
Write-Host "==> Releasing wmole $tag" -ForegroundColor Cyan

# --- 1. Bump installer default version ---
$iss = Join-Path $repoRoot "installer/wmole.iss"
(Get-Content $iss) `
  -replace '#define MyAppVersion "[^"]+"', "#define MyAppVersion `"$Version`"" `
  | Set-Content $iss -Encoding UTF8

# --- 2. Commit + push ---
if (-not $SkipPush) {
  git add -A
  if (git diff --cached --quiet) {
    Write-Host "   no changes to commit"
  } else {
    git commit -m "release: $tag" | Out-Null
  }
  git push
}

# --- 3. Build EXE + installer ---
if (-not $SkipBuild) {
  & (Join-Path $PSScriptRoot "build-windows-release.ps1") -Version $Version
}

$installer = Join-Path $repoRoot "release/wmole-$Version-setup.exe"
$exe       = Join-Path $repoRoot "dist/wmole.exe"
foreach ($p in @($installer, $exe)) {
  if (-not (Test-Path $p)) { throw "Missing artifact: $p" }
}

# --- 4. Resolve release notes ---
if ($NotesFile -and (Test-Path $NotesFile)) {
  $Notes = Get-Content $NotesFile -Raw
}
if (-not $Notes) {
  $Notes = "Automated release $tag."
}
$notesPath = Join-Path $env:TEMP "wmole-release-$Version.md"
$Notes | Set-Content $notesPath -Encoding UTF8

# --- 5. Create GitHub release (replace if tag exists) ---
git tag -f $tag | Out-Null
git push origin $tag --force | Out-Null

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$null = gh release view $tag 2>&1
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEAP
if ($exists) {
  Write-Host "   release $tag exists; deleting before recreate"
  gh release delete $tag --yes --cleanup-tag | Out-Null
  git push origin $tag --force | Out-Null
}

$ghArgs = @("release", "create", $tag, $installer, $exe,
            "--title", "wmole $tag", "--notes-file", $notesPath)
if ($Prerelease) { $ghArgs += "--prerelease" }
& gh @ghArgs

Write-Host ""
Write-Host "==> Done." -ForegroundColor Green
Write-Host "    Site changelog will pick up $tag on next page load (GitHub API)."
Write-Host "    Live:  https://wmole.vercel.app/#changelog"
