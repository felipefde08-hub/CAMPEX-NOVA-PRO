param(
  [string]$ProjectDir = (Resolve-Path "$PSScriptRoot\..\..").Path,
  [string]$Python = "",
  [string]$Executable = ""
)

$DefaultExecutable = Join-Path $ProjectDir "dist\CampexNode\CampexNode.exe"
if ([string]::IsNullOrWhiteSpace($Executable) -and (Test-Path $DefaultExecutable)) {
  $Executable = $DefaultExecutable
}

if (-not [string]::IsNullOrWhiteSpace($Executable)) {
  if (!(Test-Path $Executable)) {
    Write-Error "Executavel nao encontrado em $Executable"
    exit 1
  }
  $Action = New-ScheduledTaskAction -Execute $Executable -Argument "--no-browser --host 127.0.0.1 --port 8787" -WorkingDirectory (Split-Path $Executable -Parent)
} else {
  if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
  }
  if (!(Test-Path $Python)) {
    Write-Error "Python nao encontrado em $Python. Informe -Python C:\caminho\python.exe ou gere o executavel primeiro."
    exit 1
  }
  $Action = New-ScheduledTaskAction -Execute $Python -Argument "-m campex_node.desktop_launcher --no-browser --host 127.0.0.1 --port 8787" -WorkingDirectory $ProjectDir
}

$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "CAMPEX Node" -Action $Action -Trigger $Trigger -Settings $Settings -Description "CAMPEX Node local 24/7" -Force | Out-Null
Start-ScheduledTask -TaskName "CAMPEX Node"
Write-Host "CAMPEX Node instalado como tarefa agendada. Painel local: http://127.0.0.1:8787"
