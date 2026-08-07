# Останавливает бота.
#
# Порядок важен: сначала надзорный скрипт, потом сам бот. Иначе супервизор в run_bot.ps1
# увидит падение и немедленно поднимет бота заново.
#
# Убивается всё дерево процессов python, а не один PID. Причина проверена на этой машине:
# `.venv\Scripts\python.exe` здесь — стаб-редиректор (Python установлен как pythoncore-3.12-64,
# и venv не копирует интерпретатор, а запускает его дочерним процессом). Убийство стаба
# оставляет настоящего бота живым, и он продолжает держать polling Telegram.
#
# ВАЖНО про поиск процессов. Ранняя версия искала по подстроке `*run_bot.ps1*` и убила
# собственную оболочку: в её командной строке эта подстрока тоже встретилась. По той же
# причине под нож попал бы редактор с открытым файлом. Поэтому проверяется точный признак
# запуска (`-File <путь>` / `-m tnved_bot`) и исключается текущий процесс.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root 'scripts\run_bot.ps1'

function Get-BotProcesses {
    param([string]$Name, [string]$Pattern)
    Get-CimInstance Win32_Process -Filter "Name='$Name'" |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like $Pattern }
}

$task = Get-ScheduledTask -TaskName 'TNVED_BOT' -ErrorAction SilentlyContinue
if ($task -and $task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName 'TNVED_BOT'
    Write-Host 'Задача планировщика остановлена.'
    Start-Sleep -Seconds 1
}

# Надзорный скрипт — первым. Признак запуска, а не любое упоминание имени файла.
foreach ($proc in Get-BotProcesses -Name 'powershell.exe' -Pattern "*-File*$runner*") {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "Остановлен надзорный процесс PID $($proc.ProcessId)"
}

$bots = @(Get-BotProcesses -Name 'python.exe' -Pattern '*-m tnved_bot*')
if ($bots.Count -eq 0) {
    Write-Host 'Процессы бота не найдены.'
} else {
    foreach ($proc in $bots) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Остановлен PID $($proc.ProcessId)"
    }
    Start-Sleep -Seconds 2
}

$left = @(Get-BotProcesses -Name 'python.exe' -Pattern '*-m tnved_bot*')
if ($left.Count -gt 0) {
    Write-Warning "Осталось процессов: $($left.Count)"
    exit 1
}

# PID-файл остаётся только после аварийного завершения; лок ОС снимает сама.
Remove-Item (Join-Path $root 'data\bot.pid') -Force -ErrorAction SilentlyContinue
Write-Host 'Бот остановлен.'
