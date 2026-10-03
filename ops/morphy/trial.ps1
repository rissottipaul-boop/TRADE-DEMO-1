param(
    [ValidateSet('start','status','stop')][string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
$morphyProject = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$morphyTrial = Join-Path $morphyProject 'data/morphy-eval'
$morphyPackage = Join-Path $morphyTrial 'package'
$morphyPidFile = Join-Path $morphyTrial 'trial-process.json'
$morphyEntry = Join-Path $morphyPackage 'supervisor/index.ts'
$morphyBootstrap = Join-Path $PSScriptRoot 'bootstrap.mjs'
$morphyBootstrapUrl = ([System.Uri]$morphyBootstrap).AbsoluteUri
$morphyNode = Join-Path $morphyTrial 'node-v22.23.3-win-x64/node.exe'
$morphyConfig = Join-Path $morphyTrial 'home/.morphy/config.json'

function Get-TrialProcess {
    if (-not (Test-Path -LiteralPath $morphyPidFile)) { return $null }
    $morphySaved = Get-Content -LiteralPath $morphyPidFile -Raw | ConvertFrom-Json
    $morphyProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$morphySaved.pid)"
    if (-not $morphyProcess) { return $null }
    if ($morphyProcess.ExecutablePath -ne $morphyNode -or
        -not $morphyProcess.CommandLine.Contains($morphyEntry) -or
        -not $morphyProcess.CommandLine.Contains($morphyBootstrapUrl)) {
        throw 'PID принадлежит другому процессу; никаких действий не выполнено.'
    }
    return $morphyProcess
}

$morphyRunning = Get-TrialProcess
if ($Action -eq 'status') {
    if ($morphyRunning) { "Morphy trial: PID $($morphyRunning.ProcessId), http://127.0.0.1:7480" }
    else { 'Morphy trial: остановлен' }
    return
}
if ($Action -eq 'stop') {
    if ($morphyRunning) {
        # Завершаем только доказанный процесс пробной копии и его потомков.
        & taskkill.exe /PID $morphyRunning.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось остановить пробную копию' }
    }
    'Morphy trial: остановлен; данные сохранены'
    return
}
if ($morphyRunning) { "Уже запущен: http://127.0.0.1:7480"; return }
foreach ($morphyRequired in @($morphyNode,$morphyEntry,$morphyBootstrap,$morphyConfig)) {
    if (-not (Test-Path -LiteralPath $morphyRequired)) { throw "Не найдено: $morphyRequired" }
}
$morphySettings = Get-Content -LiteralPath $morphyConfig -Raw | ConvertFrom-Json
if ($morphySettings.port -ne 7480 -or $morphySettings.tunnel.mode -ne 'off' -or
    $morphySettings.relay.token -or $morphySettings.wallet -or
    $morphySettings.ai.provider -notin @('','openai','anthropic','ollama','pi')) {
    throw 'Launcher допускает только локальную копию без relay и кошелька; настройки аккаунта не меняются.'
}
foreach ($morphyPort in @(7480,7482,7484)) {
    if (Get-NetTCPConnection -LocalPort $morphyPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Порт $morphyPort занят; чужой процесс не остановлен."
    }
}
$morphyOldOptions = $env:NODE_OPTIONS
$morphyOldRealHome = $env:MORPHY_REAL_HOME
try {
    $env:NODE_OPTIONS = '--import="' + $morphyBootstrapUrl + '"'
    $env:MORPHY_REAL_HOME = Join-Path $morphyTrial 'home'
    $morphyArgs = @('--import', ('"' + $morphyBootstrapUrl + '"'), '--import', 'tsx/esm', ('"' + $morphyEntry + '"'))
    $morphyStarted = Start-Process -FilePath $morphyNode -ArgumentList $morphyArgs -WorkingDirectory $morphyPackage -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $morphyProject 'logs/morphy-trial.out.log') -RedirectStandardError (Join-Path $morphyProject 'logs/morphy-trial.err.log')
    @{pid=$morphyStarted.Id; started_at=[DateTimeOffset]::Now.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $morphyPidFile -Encoding utf8
} finally {
    $env:NODE_OPTIONS = $morphyOldOptions
    $env:MORPHY_REAL_HOME = $morphyOldRealHome
}
"Morphy trial запущен, PID $($morphyStarted.Id). Проверка: ops/morphy/trial.ps1 status"
'Адрес после загрузки: http://127.0.0.1:7480'
