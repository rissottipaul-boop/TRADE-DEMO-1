<#
Управление движком как фоновым процессом — переживает закрытие VS Code и сессии агента.

    ops\engine.ps1 start    # запустить (если не запущен): лог logs\engine.log, PID в data\engine.pid
                            # старые engine.log и engine.log.out — в logs\engine_<время>.log(.out)
    ops\engine.ps1 status   # жив ли процесс + метрики (src.ops status) + хвост лога
    ops\engine.ps1 stop     # штатная остановка через флаг data\STOP_ENGINE (ждёт до 30 с,
                            # потом завершает лаунчер и дочерний интерпретатор)

Все три действия трогают только движок этого каталога (ENGINE-PID-GUARD): PID из data\engine.pid —
лаунчер .venv\Scripts\python.exe этого корня с src.engine в командной строке. Чужой PID (копия
проекта, повторно выданный PID) — предупреждение, процесс не трогаем, устаревший pid-файл удаляем.

Аварийная остановка ТОРГОВЛИ — не здесь, а `python -m src.ops kill "причина"`.
#>
param([ValidateSet("start", "stop", "status")][string]$Action = "status")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root "data\engine.pid"
$StopFlag = Join-Path $Root "data\STOP_ENGINE"
$Log = Join-Path $Root "logs\engine.log"

# ENGINE-PID-GUARD: движок ЭТОГО каталога — лаунчер $Py (свой .venv) с src.engine в командной строке.
# 25.09 копия проекта с тем же data\engine.pid выполнила stop и убила наш движок: проверки одной
# командной строки мало, она у всех копий одинаковая. Чужой PID не трогаем, pid-файл удаляем.
$script:ForeignPid = $null

function Get-EngineProcess {
    $script:ForeignPid = $null
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = "$(Get-Content $PidFile -Raw)".Trim()
    if ($raw -notmatch '^\d+$') { $script:ForeignPid = "в data\engine.pid не PID: '$raw'"; return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$raw" -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }  # процесса нет — движок мёртв; pid-файл оставляем сторожу
    $own = $proc.CommandLine -match "src\.engine" -and $proc.ExecutablePath -and
        [string]::Equals([IO.Path]::GetFullPath($proc.ExecutablePath), [IO.Path]::GetFullPath($Py),
            [StringComparison]::OrdinalIgnoreCase)
    if ($own) { return $proc }
    $script:ForeignPid = "PID $raw — не движок этого каталога: $($proc.ExecutablePath) | $($proc.CommandLine)"
    return $null
}

function Clear-ForeignPid {
    # Предупреждение и удаление устаревшего pid-файла; сам процесс не трогаем
    if (-not $script:ForeignPid) { return }
    Write-Output "ВНИМАНИЕ: $($script:ForeignPid). Процесс не трогаю, устаревший data\engine.pid удалён"
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    $script:ForeignPid = $null
}

New-Item -ItemType Directory -Force (Join-Path $Root "data"), (Join-Path $Root "logs") | Out-Null

switch ($Action) {
    "start" {
        # Стартовать могут человек, автозапуск при входе и сторож (ENGINE-WATCHDOG) —
        # мьютекс не даёт двум вызовам одновременно увидеть «не запущен» и поднять два движка
        $mutex = [System.Threading.Mutex]::new($false, "Local\OKX-Bot-engine-start")
        try { $owned = $mutex.WaitOne(120000) }
        catch [System.Threading.AbandonedMutexException] { $owned = $true }  # прошлый держатель умер
        if (-not $owned) { Write-Output "Другой запуск движка идёт дольше 2 мин — пропускаю"; exit 1 }
        try {
            $running = Get-EngineProcess
            if ($running) { Write-Output "Движок уже работает: PID $($running.ProcessId)"; exit 0 }
            Clear-ForeignPid
            if (Test-Path $StopFlag) {
                Remove-Item $StopFlag -Force
                Write-Output "Снят устаревший флаг data\STOP_ENGINE (остался от предыдущей остановки)"
            }
            # Ротация: старые лог и stdout сохраняем, а не затираем редиректом — они нужны для разбора
            # инцидентов (ENGINE-WATCHDOG: 25.09 .out затёрли, зависание от убийства не отличить)
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            foreach ($pair in @(@($Log, ".log"), @("$Log.out", ".log.out"))) {
                $file = $pair[0]
                if ((Test-Path $file) -and (Get-Item $file).Length -gt 0) {
                    $archived = Join-Path $Root ("logs\engine_{0}{1}" -f $stamp, $pair[1])
                    Move-Item $file $archived
                    Write-Output "Предыдущий лог сохранён: $archived"
                }
            }
            $proc = Start-Process -FilePath $Py -ArgumentList "-m", "src.engine" -WorkingDirectory $Root `
                -RedirectStandardError $Log -RedirectStandardOutput "$Log.out" -WindowStyle Hidden -PassThru
            Set-Content -Path $PidFile -Value $proc.Id
            Start-Sleep -Seconds 8
            if (Get-EngineProcess) { Write-Output "Движок запущен: PID $($proc.Id), лог $Log" }
            else { Write-Output "Движок завершился сразу после старта — см. $Log"; Get-Content $Log -Tail 20; exit 1 }
        }
        finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
    }
    "stop" {
        $running = Get-EngineProcess
        if (-not $running) { Clear-ForeignPid; Write-Output "Движок не запущен"; exit 0 }
        # Метка времени в содержимом флага — источник и момент остановки видны при разборе инцидентов
        Set-Content -Path $StopFlag -Value ("ops/engine.ps1 stop {0}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"))
        for ($i = 0; $i -lt 15 -and (Get-EngineProcess); $i++) { Start-Sleep -Seconds 2 }
        if (Get-EngineProcess) {
            # В PID — лаунчер .venv\Scripts\python.exe, движок — его дочерний интерпретатор:
            # без него убитый лаунчер оставил бы зависший движок сиротой
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($running.ProcessId)" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match "src\.engine" })
            Write-Output ("Не остановился за 30 с — завершаю PID {0}{1}" -f $running.ProcessId,
                ($(if ($children) { " и дочерний " + ($children.ProcessId -join ", ") } else { "" })))
            foreach ($child in $children) { Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $running.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Write-Output "Движок остановлен"
    }
    "status" {
        $running = Get-EngineProcess
        if ($running) { Write-Output "Движок работает: PID $($running.ProcessId), с $($running.CreationDate)" }
        else { Clear-ForeignPid; Write-Output "Движок НЕ запущен" }
        Push-Location $Root
        try { & $Py -m src.ops status } finally { Pop-Location }
        if (Test-Path $Log) { Write-Output "--- хвост $Log"; Get-Content $Log -Tail 8 }
    }
}
