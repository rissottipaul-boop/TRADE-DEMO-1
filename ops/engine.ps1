<#
Управление движком как фоновым процессом — переживает закрытие VS Code и сессии агента.

    ops\engine.ps1 start    # запустить (если не запущен): лог logs\engine.log, PID в data\engine.pid
    ops\engine.ps1 status   # жив ли процесс + метрики (src.ops status) + хвост лога
    ops\engine.ps1 stop     # штатная остановка через флаг data\STOP_ENGINE (ждёт до 30 с)

Аварийная остановка ТОРГОВЛИ — не здесь, а `python -m src.ops kill "причина"`.
#>
param([ValidateSet("start", "stop", "status")][string]$Action = "status")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root "data\engine.pid"
$StopFlag = Join-Path $Root "data\STOP_ENGINE"
$Log = Join-Path $Root "logs\engine.log"

function Get-EngineProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $enginePid = [int](Get-Content $PidFile -Raw)
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$enginePid" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -match "src\.engine") { return $proc }
    return $null
}

New-Item -ItemType Directory -Force (Join-Path $Root "data"), (Join-Path $Root "logs") | Out-Null

switch ($Action) {
    "start" {
        $running = Get-EngineProcess
        if ($running) { Write-Output "Движок уже работает: PID $($running.ProcessId)"; exit 0 }
        if (Test-Path $StopFlag) {
            Remove-Item $StopFlag -Force
            Write-Output "Снят устаревший флаг data\STOP_ENGINE (остался от предыдущей остановки)"
        }
        # Ротация: старый лог сохраняем, а не затираем редиректом — он нужен для разбора инцидентов
        if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 0) {
            $archived = Join-Path $Root ("logs\engine_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
            Move-Item $Log $archived
            Write-Output "Предыдущий лог сохранён: $archived"
        }
        $proc = Start-Process -FilePath $Py -ArgumentList "-m", "src.engine" -WorkingDirectory $Root `
            -RedirectStandardError $Log -RedirectStandardOutput "$Log.out" -WindowStyle Hidden -PassThru
        Set-Content -Path $PidFile -Value $proc.Id
        Start-Sleep -Seconds 8
        if (Get-EngineProcess) { Write-Output "Движок запущен: PID $($proc.Id), лог $Log" }
        else { Write-Output "Движок завершился сразу после старта — см. $Log"; Get-Content $Log -Tail 20; exit 1 }
    }
    "stop" {
        $running = Get-EngineProcess
        if (-not $running) { Write-Output "Движок не запущен"; exit 0 }
        # Метка времени в содержимом флага — источник и момент остановки видны при разборе инцидентов
        Set-Content -Path $StopFlag -Value ("ops/engine.ps1 stop {0}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"))
        for ($i = 0; $i -lt 15 -and (Get-EngineProcess); $i++) { Start-Sleep -Seconds 2 }
        if (Get-EngineProcess) {
            Write-Output "Не остановился за 30 с — завершаю PID $($running.ProcessId)"
            Stop-Process -Id $running.ProcessId -Force
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Write-Output "Движок остановлен"
    }
    "status" {
        $running = Get-EngineProcess
        if ($running) { Write-Output "Движок работает: PID $($running.ProcessId), с $($running.CreationDate)" }
        else { Write-Output "Движок НЕ запущен" }
        Push-Location $Root
        try { & $Py -m src.ops status } finally { Pop-Location }
        if (Test-Path $Log) { Write-Output "--- хвост $Log"; Get-Content $Log -Tail 8 }
    }
}
