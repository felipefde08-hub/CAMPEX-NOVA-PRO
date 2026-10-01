param(
  [string]$Python = "python",
  [switch]$SkipInstall,
  # Code signing (optional): a .pfx file or a certificate already installed in
  # the Windows store. The .pfx password comes from CAMPEX_SIGN_PFX_PASSWORD so
  # it never lands in shell history or in the repository.
  [string]$CertificatePath = $env:CAMPEX_SIGN_PFX_PATH,
  [string]$CertificateThumbprint = $env:CAMPEX_SIGN_CERT_THUMBPRINT,
  [string]$TimestampServer = "http://timestamp.digicert.com"
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

function Get-SigningCertificate {
  if ($CertificatePath) {
    if (-not (Test-Path $CertificatePath)) {
      throw "Certificado nao encontrado em $CertificatePath"
    }
    if ($env:CAMPEX_SIGN_PFX_PASSWORD) {
      $secure = ConvertTo-SecureString $env:CAMPEX_SIGN_PFX_PASSWORD -AsPlainText -Force
      $pfx = Get-PfxData -FilePath $CertificatePath -Password $secure
    } else {
      $pfx = Get-PfxData -FilePath $CertificatePath
    }
    $cert = $pfx.EndEntityCertificates | Select-Object -First 1
    if ($cert -and -not $cert.HasPrivateKey) {
      # Get-PfxData does not always expose the key; load the file directly.
      $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable
      $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
        $CertificatePath, $env:CAMPEX_SIGN_PFX_PASSWORD, $flags)
    }
    return $cert
  }
  if ($CertificateThumbprint) {
    $thumb = ($CertificateThumbprint -replace '\s', '').ToUpper()
    $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert -ErrorAction SilentlyContinue |
      Where-Object { $_.Thumbprint -eq $thumb } | Select-Object -First 1
    if (-not $cert) {
      throw "Certificado de assinatura $thumb nao encontrado em CurrentUser\My ou LocalMachine\My."
    }
    return $cert
  }
  return $null
}

$SigningCertificate = Get-SigningCertificate
if ($SigningCertificate) {
  if (-not $SigningCertificate.HasPrivateKey) {
    throw "O certificado $($SigningCertificate.Subject) nao tem chave privada; nao e possivel assinar."
  }
  Write-Host "Assinando $ExePath com $($SigningCertificate.Subject)"
  $signature = Set-AuthenticodeSignature -FilePath $ExePath -Certificate $SigningCertificate `
    -HashAlgorithm SHA256 -TimestampServer $TimestampServer -IncludeChain NotRoot
  if ($signature.Status -ne "Valid") {
    # Self-signed certificates end here (UnknownError/NotTrusted): they do not
    # help on client machines, so the build refuses to ship them as "signed".
    throw ("Falha ao assinar o executavel: $($signature.Status) - $($signature.StatusMessage). " +
      "Use um certificado de assinatura de codigo emitido por uma autoridade confiavel.")
  }
  Write-Host "Assinatura valida: $($signature.SignerCertificate.Thumbprint)"
} else {
  Write-Warning ("Executavel NAO assinado: antivirus e SmartScreen podem bloquear o CampexNode.exe no cliente. " +
    "Defina CAMPEX_SIGN_PFX_PATH (+ CAMPEX_SIGN_PFX_PASSWORD) ou CAMPEX_SIGN_CERT_THUMBPRINT para assinar.")
}

$ReadmePath = Join-Path $PackageDir "LEIA-ME.txt"
@"
CAMPEX Node para Windows

Como usar:
1. Extraia este .zip em uma pasta local.
2. Abra CampexNode.exe. Mantenha CampexNode.exe e a pasta _internal juntos e
   do mesmo pacote; nao copie arquivos de versoes diferentes por cima.
3. O painel local abrira em http://127.0.0.1:8787.
4. Configure/conecte o Node e cadastre as cameras.

Se aparecer "O Windows nao pode acessar o dispositivo, caminho ou arquivo
especificado", o antivirus ou o bloqueio de arquivos baixados impediu a
execucao. Em um PowerShell como administrador:
  Get-ChildItem C:\CampexNode -Recurse | Unblock-File
Se o Defender removeu o arquivo, restaure em Seguranca do Windows > Protecao
contra virus e ameacas > Historico de protecao, adicione a pasta em Exclusoes
e extraia o .zip novamente.

Esta versao inclui Edge Vision offline inicial com OpenCV HOG para deteccao
local de pessoas nas cameras com Vision ativada.

Dados, logs e credenciais locais ficam em:
%LOCALAPPDATA%\CAMPEX\node

Para rodar em segundo plano ao entrar no Windows, use o instalador de tarefa
agendada disponivel no repositorio em packaging\windows\install_task.ps1.
"@ | Set-Content -LiteralPath $ReadmePath -Encoding UTF8

Compress-Archive -LiteralPath $PackageDir -DestinationPath $ZipPath -Force

Write-Host "CAMPEX Node criado em: $ExePath"
Write-Host "Pacote ZIP criado em: $ZipPath"
