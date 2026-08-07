# Удаляет автозапуск бота. Сам бот при этом останавливается — иначе он продолжил бы
# работать до перезагрузки, а задачи для его остановки уже не было бы.

$ErrorActionPreference = 'Stop'
$taskName = 'TNVED_BOT'

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Задачи '$taskName' нет — удалять нечего."
} else {
    if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Задача '$taskName' удалена."
}

& (Join-Path $PSScriptRoot 'stop_bot.ps1')
