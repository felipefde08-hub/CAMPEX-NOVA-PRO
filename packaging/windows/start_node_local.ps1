param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8787,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $RepoRoot

Write-Host "CAMPEX Node local"
Write-Host "Repositorio: $RepoRoot"
Write-Host "Porta: $Port"

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $processId = $listener.OwningProcess
    if ($processId -and $processId -ne $PID) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            Write-Host "Encerrando processo antigo na porta ${Port}: $($process.ProcessName) ($processId)"
            Stop-Process -Id $processId -Force
        }
    }
}

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

Write-Host "Python: $python"
Write-Host "URL do Node: http://$HostAddress`:$Port"

$argsList = @("-m", "campex_node.main", "--app", "--host", $HostAddress, "--port", "$Port")
if (-not $OpenBrowser) {
    $argsList += "--no-browser"
}

& $python @argsList
