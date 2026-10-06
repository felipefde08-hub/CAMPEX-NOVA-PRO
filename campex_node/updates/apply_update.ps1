# Replaces an installed CAMPEX Node folder with a verified new version.
#
# Started (detached) by the running Node, which then exits. This script waits
# for it, swaps the folders, starts the new version and rolls back to the old
# one if the new version does not answer on the local panel in time.
param(
  [Parameter(Mandatory = $true)][int]$NodeProcessId,
  [Parameter(Mandatory = $true)][string]$Source,
  [Parameter(Mandatory = $true)][string]$Target,
  [Parameter(Mandatory = $true)][string]$Version,
  [Parameter(Mandatory = $true)][string]$PreviousVersion,
  [Parameter(Mandatory = $true)][string]$StatusUrl,
  [Parameter(Mandatory = $true)][string]$ResultPath,
  [Parameter(Mandatory = $true)][string]$LogPath,
  # Written by the restarted Node with the port it chose; wins over StatusUrl.
  [string]$PortFile = "",
  [int]$HealthTimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
$ExeName = "CampexNode.exe"
$TaskName = "CAMPEX Node"
$Backup = "$Target.previous"

function Write-Log([string]$Message) {
  $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
  Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Write-Result([string]$Status, [string]$Detail) {
  @{
    status = $Status
    version = $Version
    previous_version = $PreviousVersion
    detail = $Detail
    finished_at = (Get-Date).ToUniversalTime().ToString("o")
  } | ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding UTF8
}

function Start-Node([string]$Folder) {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task -and $task.Actions[0].Execute -like "*$ExeName") {
    Start-ScheduledTask -TaskName $TaskName
  } else {
    Start-Process -FilePath (Join-Path $Folder $ExeName) -ArgumentList "--no-browser" -WorkingDirectory $Folder
  }
}

function Stop-NodeFrom([string]$Folder) {
  $exe = Join-Path $Folder $ExeName
  Get-Process -Name "CampexNode" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $exe } |
    Stop-Process -Force -ErrorAction SilentlyContinue
}

function Get-StatusUrl {
  if ($PortFile -and (Test-Path -LiteralPath $PortFile)) {
    $port = (Get-Content -LiteralPath $PortFile -Raw -ErrorAction SilentlyContinue)
    if ($port -and $port.Trim() -match '^\d+$') { return "http://127.0.0.1:$($port.Trim())/api/status" }
  }
  return $StatusUrl
}

function Wait-NodeVersion([string]$Expected) {
  $deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $status = Invoke-RestMethod -Uri (Get-StatusUrl) -TimeoutSec 5 -UseBasicParsing
      if ($status.version -eq $Expected) { return $true }
    } catch { }
    Start-Sleep -Seconds 3
  }
  return $false
}

function Move-WithRetry([string]$From, [string]$To) {
  for ($attempt = 1; $attempt -le 10; $attempt++) {
    try {
      Move-Item -LiteralPath $From -Destination $To
      return
    } catch {
      if ($attempt -eq 10) { throw }
      Start-Sleep -Seconds 2
    }
  }
}

try {
  Write-Log "update ${PreviousVersion} -> ${Version}, waiting for Node process $NodeProcessId"
  $process = Get-Process -Id $NodeProcessId -ErrorAction SilentlyContinue
  if ($process -and -not $process.WaitForExit(60000)) {
    Write-Log "Node did not exit in 60s; stopping it"
    Stop-Process -Id $NodeProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
  }

  if (Test-Path -LiteralPath $Backup) {
    Remove-Item -LiteralPath $Backup -Recurse -Force
  }
  Move-WithRetry $Target $Backup
  try {
    Move-WithRetry $Source $Target
  } catch {
    Move-WithRetry $Backup $Target
    throw
  }
  Write-Log "folders swapped; starting $Version"
  Start-Node $Target

  if (Wait-NodeVersion $Version) {
    Write-Log "update to $Version succeeded"
    Write-Result "ok" ""
    Remove-Item -LiteralPath $Backup -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
  }

  Write-Log "$Version did not answer on $StatusUrl; rolling back to $PreviousVersion"
  Stop-NodeFrom $Target
  Start-Sleep -Seconds 2
  Remove-Item -LiteralPath $Target -Recurse -Force
  Move-WithRetry $Backup $Target
  Start-Node $Target
  Write-Result "rolled_back" "new version did not answer on the local panel"
  exit 1
} catch {
  Write-Log "update failed: $($_.Exception.Message)"
  Write-Result "failed" $_.Exception.Message
  if (-not (Test-Path -LiteralPath (Join-Path $Target $ExeName)) -and (Test-Path -LiteralPath $Backup)) {
    Move-Item -LiteralPath $Backup -Destination $Target -ErrorAction SilentlyContinue
  }
  # Never leave the site without a running Node.
  Start-Node $Target
  exit 1
}
