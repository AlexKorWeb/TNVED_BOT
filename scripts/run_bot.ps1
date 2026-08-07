# Запуск бота с надзором. Используется задачей планировщика и вручную.
#
# Перезапуск делает сам этот скрипт, а не планировщик задач. Причина проверена: при
# принудительном завершении процесса задача заканчивается с кодом 0xFFFFFFFF, но настройка
# «перезапускать при сбое» не срабатывает — бот оставался лежать. Цикл здесь надёжнее
# и не зависит от тонкостей поведения планировщика.
#
# Скрипт сам переходит в корень проекта: планировщик запускает процессы из System32,
# и без этого относительные пути вели бы туда.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Error "Не найдено виртуальное окружение: $python`nСоздайте: py -3.12 -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
    exit 2
}
if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Error "Не найден .env. Скопируйте .env.example и заполните BOT_TOKEN и ADMIN_USER_IDS."
    exit 2
}
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    # Не фатально: без claude бот работает в режиме поиска по справочнику.
    Write-Warning 'claude не найден в PATH — бот запустится без ИИ.'
}

New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null

# UTF-8 обязателен: без него русские тексты в stdout ломаются под планировщиком.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

# Коды выхода, при которых перезапуск бессмысленен:
#   0 — штатная остановка (её попросили),
#   2 — ошибка конфигурации (сама не исправится),
#   3 — бот уже запущен другим экземпляром.
$noRestart = @(0, 2, 3)
$delaySeconds = 10
$maxDelay = 300

while ($true) {
    & $python -m tnved_bot
    $code = $LASTEXITCODE

    if ($noRestart -contains $code) {
        Write-Host "Бот завершился с кодом $code — перезапуск не требуется."
        exit $code
    }

    Write-Warning "Бот упал с кодом $code. Перезапуск через $delaySeconds с."
    Start-Sleep -Seconds $delaySeconds
    # Пауза растёт: если падение вызвано устойчивой причиной, не крутим цикл впустую.
    $delaySeconds = [Math]::Min($delaySeconds * 2, $maxDelay)
}
