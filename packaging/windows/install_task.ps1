param(
  [string]$ProjectDir = (Resolve-Path "$PSScriptRoot\..\..").Path,
  [string]$Python = ""
)

if ([string]::IsNullOrWhiteSpace($Python)) {
  $Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}
if (!(Test-Path $Python)) {
  Write-Error "Python não encontrado em $Python. Informe -Python C:\caminho\python.exe"
  exit 1
}

$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m campex_node.main --app --host 127.0.0.1 --port 8787" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "CAMPEX Node" -Action $Action -Trigger $Trigger -Settings $Settings -Description "CAMPEX Node local 24/7" -Force | Out-Null
Start-ScheduledTask -TaskName "CAMPEX Node"
Write-Host "CAMPEX Node instalado como tarefa agendada. Painel local: http://127.0.0.1:8787"
