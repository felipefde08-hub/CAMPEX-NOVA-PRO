param(
  [string]$Python = "python",
  [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path "$PSScriptRoot\..\..").Path
$SpecPath = Join-Path $ProjectDir "packaging\windows\CAMPEX-Desktop.spec"
$DistDir = Join-Path $ProjectDir "dist"
$BuildDir = Join-Path $ProjectDir "build"

Set-Location $ProjectDir

if (-not $SkipInstall) {
  & $Python -m pip install --upgrade pip
  & $Python -m pip install -r "campex_node\requirements.txt"
}

if (Test-Path (Join-Path $DistDir "CAMPEX-Node")) {
  Remove-Item -LiteralPath (Join-Path $DistDir "CAMPEX-Node") -Recurse -Force
}
if (Test-Path (Join-Path $BuildDir "CAMPEX-Desktop")) {
  Remove-Item -LiteralPath (Join-Path $BuildDir "CAMPEX-Desktop") -Recurse -Force
}

& $Python -m PyInstaller --noconfirm --clean $SpecPath

$ExePath = Join-Path $DistDir "CAMPEX-Node\CAMPEX-Node.exe"
if (-not (Test-Path $ExePath)) {
  throw "Build finalizado, mas o executavel nao foi encontrado em $ExePath"
}

Write-Host "CAMPEX Node criado em: $ExePath"
