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

# go2rtc ships inside the Node; it only runs with CAMPEX_NODE_GO2RTC=1.
& $Python scripts\fetch_go2rtc.py
if ($LASTEXITCODE -ne 0) {
  throw "Falha ao baixar o go2rtc."
}

$PublicKeyPath = Join-Path $ProjectDir "campex_node\updates\release_public_key.txt"
if (-not (Test-Path $PublicKeyPath)) {
  Write-Warning ("Sem chave publica de atualizacao: este Node NAO vai se atualizar sozinho. " +
    "Rode uma vez: $Python scripts\release_node.py keygen")
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
3. O painel abre em http://127.0.0.1:8787/app neste computador.
4. No primeiro acesso, crie a conta de administrador (so neste computador).
   Depois, cadastre a equipe em Configuracoes > Usuarios.
5. Cadastre as cameras, desenhe as zonas e os turnos em Fabrica > Configuracao.

Acesso pela rede da fabrica:
- Outros computadores abrem http://IP-DESTE-COMPUTADOR:8787/app e entram com
  login. Na primeira execucao o Windows pergunta se permite o acesso pela
  rede: aceite para liberar.
- Para deixar o painel so neste computador, defina a variavel de ambiente
  CAMPEX_NODE_HOST=127.0.0.1.

Gravacao continua e backups:
- O Node grava as cameras em %LOCALAPPDATA%\CAMPEX\node\recordings e para de
  gravar quando o disco fica com menos de 10 GB livres. Para gravar em outro
  disco, defina CAMPEX_NODE_RECORDING_DIR (ex.: D:\CampexGravacoes).
- Uma copia do banco e feita todo dia em %LOCALAPPDATA%\CAMPEX\node\backups.
  Para guardar em outro disco, defina CAMPEX_NODE_BACKUP_DIR.

Se aparecer "O Windows nao pode acessar o dispositivo, caminho ou arquivo
especificado", o antivirus ou o bloqueio de arquivos baixados impediu a
execucao. Em um PowerShell como administrador:
  Get-ChildItem C:\CampexNode -Recurse | Unblock-File
Se o Defender removeu o arquivo, restaure em Seguranca do Windows > Protecao
contra virus e ameacas > Historico de protecao, adicione a pasta em Exclusoes
e extraia o .zip novamente.

Dados, logs, contas e configuracoes ficam em:
%LOCALAPPDATA%\CAMPEX\node

Para rodar em segundo plano ao entrar no Windows, use o instalador de tarefa
agendada disponivel no repositorio em packaging\windows\install_task.ps1.
"@ | Set-Content -LiteralPath $ReadmePath -Encoding UTF8

Compress-Archive -LiteralPath $PackageDir -DestinationPath $ZipPath -Force

Write-Host "CAMPEX Node criado em: $ExePath"
Write-Host "Pacote ZIP criado em: $ZipPath"

if (Test-Path $PublicKeyPath) {
  # Signed manifest read by installed Nodes to update themselves.
  & $Python scripts\release_node.py manifest $ZipPath
  if ($LASTEXITCODE -ne 0) {
    throw "Falha ao gerar o manifesto assinado da atualizacao."
  }
}
