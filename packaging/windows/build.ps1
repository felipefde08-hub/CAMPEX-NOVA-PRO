param(
  [string]$Python = "python",
  [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path "$PSScriptRoot\..\..").Path
$SpecPath = Join-Path $ProjectDir "packaging\windows\CAMPEX-Desktop.spec"
$DistDir = Join-Path $ProjectDir "dist"
$BuildDir = Join-Path $ProjectDir "build"
$PackageName = "CampexNode"
$PackageDir = Join-Path $DistDir $PackageName
$ExePath = Join-Path $PackageDir "CampexNode.exe"
$ZipPath = Join-Path $DistDir "$PackageName-windows.zip"

Set-Location $ProjectDir

if (-not $SkipInstall) {
  & $Python -m pip install --upgrade pip
  & $Python -m pip install -r "campex_node\requirements.txt"
}

if (Test-Path $PackageDir) {
  Remove-Item -LiteralPath $PackageDir -Recurse -Force
}
if (Test-Path $ZipPath) {
  Remove-Item -LiteralPath $ZipPath -Force
}
if (Test-Path (Join-Path $BuildDir "CAMPEX-Desktop")) {
  Remove-Item -LiteralPath (Join-Path $BuildDir "CAMPEX-Desktop") -Recurse -Force
}

& $Python -m PyInstaller --noconfirm --clean $SpecPath

if (-not (Test-Path $ExePath)) {
  throw "Build finalizado, mas o executavel nao foi encontrado em $ExePath"
}

$ReadmePath = Join-Path $PackageDir "LEIA-ME.txt"
@"
CAMPEX Node para Windows

Como usar:
1. Extraia este .zip em uma pasta local.
2. Abra CampexNode.exe.
3. O painel local abrira em http://127.0.0.1:8787.
4. Configure/conecte o Node e cadastre as cameras.

Dados, logs e credenciais locais ficam em:
%LOCALAPPDATA%\CAMPEX\node

Para rodar em segundo plano ao entrar no Windows, use o instalador de tarefa
agendada disponivel no repositorio em packaging\windows\install_task.ps1.
"@ | Set-Content -LiteralPath $ReadmePath -Encoding UTF8

Compress-Archive -LiteralPath $PackageDir -DestinationPath $ZipPath -Force

Write-Host "CAMPEX Node criado em: $ExePath"
Write-Host "Pacote ZIP criado em: $ZipPath"
