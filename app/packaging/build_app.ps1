# Build the td-deploy Studio desktop app on Windows.
#   powershell -ExecutionPolicy Bypass -File app\packaging\build_app.ps1
# Steps: freeze the sidecar (PyInstaller) -> app\dist -> electron-builder (nsis).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$app  = (Resolve-Path "$here\..").Path
$repo = (Resolve-Path "$app\..").Path

Write-Host "==> stamp base image tag"
$tag = if ($env:BASE_IMAGE_TAG) { $env:BASE_IMAGE_TAG } else { "latest" }
"{`"base_image_tag`": `"$tag`"}" | Out-File -Encoding utf8 "$app\version.json"

Write-Host "==> python deps"
python -m pip install -r "$here\requirements.txt"

Write-Host "==> freeze sidecar (PyInstaller)"
Push-Location $repo
python -m PyInstaller --noconfirm --distpath "$app\dist" --workpath "$app\build" "$here\sidecar.spec"
Pop-Location

if (-not (Test-Path "$app\toolchain")) {
  Write-Warning "app\toolchain\ missing - the frozen app will fall back to Nix (unavailable on Windows)."
}

Write-Host "==> electron-builder"
Push-Location "$app\electron"
npm install
npm run dist
Pop-Location

Write-Host "==> artifacts in $app\electron\dist"
Get-ChildItem "$app\electron\dist" | Select-Object Name
