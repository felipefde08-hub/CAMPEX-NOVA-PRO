Unregister-ScheduledTask -TaskName "CAMPEX Node" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "CAMPEX Node removido da tarefa agendada. Dados locais não foram apagados."
