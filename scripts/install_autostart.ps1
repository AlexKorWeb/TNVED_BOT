# Регистрирует автозапуск бота в Планировщике заданий Windows.
#
# Задача запускается при входе пользователя в систему, а не при загрузке компьютера:
# claude CLI работает под учётной записью пользователя с его авторизацией, и в сеансе
# службы он недоступен.

param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$taskName = 'TNVED_BOT'
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root 'scripts\run_bot.ps1'

if (-not (Test-Path $runner)) { Write-Error "Не найден $runner"; exit 1 }

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Write-Host "Задача '$taskName' уже существует."
    Write-Host "Перезаписать: .\scripts\install_autostart.ps1 -Force"
    exit 0
}
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Прежняя задача удалена."
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

# Бот работает бессрочно: ограничение по времени выполнения его бы убивало.
$settings.ExecutionTimeLimit = 'PT0S'

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Telegram-бот подбора кодов ТН ВЭД ЕАЭС' | Out-Null

Write-Host "Задача '$taskName' создана: запуск при входе в систему, перезапуск 3 раза с интервалом 1 мин."
Write-Host ""
Write-Host "Запустить сейчас:      Start-ScheduledTask -TaskName $taskName"
Write-Host "Проверить состояние:   Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo"
Write-Host "Остановить бота:       .\scripts\stop_bot.ps1"
Write-Host "Удалить автозапуск:    .\scripts\uninstall_autostart.ps1"
