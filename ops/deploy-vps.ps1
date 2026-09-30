<#
ops/deploy-vps.ps1 — Скрипт подготовки и диагностики окружения VPS / локального хоста.
Проверяет:
1. Синхронизацию времени (W32Time / NTP).
2. Латентность к инфраструктуре OKX (Токио).
3. Создание резервной копии баз данных (data/*.db).
4. Статус мониторинга Netdata.
#>
param(
    [string]$TargetHost = "aws.okx.com",
    [switch]$SkipBackup
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot

Write-Host "=== [1/4] Проверка службы времени (NTP) ===" -ForegroundColor Cyan
try {
    w32tm /query /status
} catch {
    Write-Warning "Не удалось запросить w32tm: $_"
}

Write-Host "`n=== [2/4] Замер сетевой задержки до $TargetHost ===" -ForegroundColor Cyan
try {
    $ping = Test-Connection -ComputerName $TargetHost -Count 4
    $avg = ($ping | Measure-Object -Property ResponseTime -Average).Average
    Write-Host "Средняя задержка до $TargetHost: $avg ms" -ForegroundColor Green
} catch {
    Write-Warning "Ошибка пинга до $TargetHost: $_"
}

if (-not $SkipBackup) {
    Write-Host "`n=== [3/4] Резервное копирование SQLite баз (data/) ===" -ForegroundColor Cyan
    $backupDir = Join-Path $Root "backups"
    if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir | Out-Null }
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    $zipPath = Join-Path $backupDir "okx_bot_backup_$ts.zip"
    $dataDir = Join-Path $Root "data"
    if (Test-Path $dataDir) {
        Compress-Archive -Path "$dataDir\*.db" -DestinationPath $zipPath -Force
        Write-Host "Резервная копия создана: $zipPath" -ForegroundColor Green
    } else {
        Write-Host "Каталог data/ пуст, бэкап пропущен." -ForegroundColor Yellow
    }
}

Write-Host "`n=== [4/4] Проверка агента мониторинга Netdata ===" -ForegroundColor Cyan
$netdataPs1 = Join-Path $PSScriptRoot "netdata.ps1"
if (Test-Path $netdataPs1) {
    & $netdataPs1 status
} else {
    Write-Warning "Скрипт ops\netdata.ps1 не найден."
}

Write-Host "`n=== Диагностика развертывания завершена ===" -ForegroundColor Cyan
