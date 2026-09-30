<#
Автоматика движка в Планировщике Windows: автозапуск при входе (ENGINE-AUTOSTART) и сторож
(ENGINE-WATCHDOG).

    ops\autostart.ps1 status       # есть ли задачи, когда запускались и с каким кодом
    ops\autostart.ps1 register     # создать или обновить обе задачи (выполняет человек)
    ops\autostart.ps1 unregister   # удалить обе задачи — движок больше не стартует и не поднимается сам
    ops\autostart.ps1 watch        # проверка сторожа вручную, как по расписанию (с перезапуском)

«OKX-Bot Engine» — при входе текущего пользователя ждёт 2 мин (сеть и VPN), затем вызывает
`ops\engine.ps1 start`. Он идемпотентен: работающий движок не трогает.
«OKX-Bot Watchdog» — раз в 5 мин `.venv\Scripts\pythonw.exe -m src.engine_watchdog --act`: процесс
из data\engine.pid — движок этого каталога и жив, logs\engine.log обновлялся ≤ 3 мин назад; иначе
повторная проверка через 90 с и `ops\engine.ps1 stop` + `start`. pythonw — без консоли: окно раз
в 5 мин не мелькает. Правила «жив / тишина / мёртв» и когда сторож не трогает движок
(data\STOP_ENGINE, нет data\engine.pid после штатного stop, лимит 3 перезапуска в час) —
в docstring src\engine_watchdog.py.

Флаги data\KILL и risk-состояние движок читает сам — автоматика торговлю не разблокирует.
Пауза обеих задач без удаления — файл data\AUTOSTART_OFF. Лог обеих — logs\autostart.log.
Права администратора не нужны: задачи работают от текущего пользователя и только когда он вошёл.
#>
param([ValidateSet("register", "unregister", "status", "run", "watch")][string]$Action = "status")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = "OKX-Bot Engine"
$WatchTaskName = "OKX-Bot Watchdog"
$OffFlag = Join-Path $Root "data\AUTOSTART_OFF"
$Log = Join-Path $Root "logs\autostart.log"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$PyW = Join-Path $Root ".venv\Scripts\pythonw.exe"

function Write-AutostartLog([string]$Text) {
    New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
    Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"), $Text)
}

switch ($Action) {
    "register" {
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

        # Нужен pwsh 7: Windows PowerShell 5.1 читает скрипты без BOM в cp1251 и ломает кириллицу.
        # Alias из WindowsApps стабилен, а путь пакета Store меняется с каждой версией.
        $pwsh = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pwsh.exe"
        if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh -ErrorAction Stop).Source }
        $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" run' -f $PSCommandPath
        $taskAction = New-ScheduledTaskAction -Execute $pwsh -Argument $arg -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $trigger.Delay = "PT2M"
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "ops\engine.ps1 start при входе в систему (ENGINE-AUTOSTART)" -Force | Out-Null
        Write-Output "Задача «$TaskName» зарегистрирована: вход пользователя + 2 мин → ops\engine.ps1 start"

        # Сторож: раз в 5 мин без срока окончания. Проверка с перезапуском укладывается в ~2,5 мин
        # (90 с повтор + stop до 35 с + start ~10 с); лимит 4 мин — меньше интервала
        if (-not (Test-Path $PyW)) { throw "Нет $PyW — сторож запускается из .venv проекта" }
        $watchAction = New-ScheduledTaskAction -Execute $PyW -Argument "-m src.engine_watchdog --act" -WorkingDirectory $Root
        $watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $watchSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $WatchTaskName -Action $watchAction -Trigger $watchTrigger -Settings $watchSettings `
            -Principal $principal -Description "Сторож движка раз в 5 мин: python -m src.engine_watchdog --act (ENGINE-WATCHDOG)" -Force | Out-Null
        Write-Output "Задача «$WatchTaskName» зарегистрирована: раз в 5 мин → src.engine_watchdog --act"
    }
    "unregister" {
        foreach ($name in $TaskName, $WatchTaskName) {
            if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $name -Confirm:$false
                Write-Output "Задача «$name» удалена"
            }
            else { Write-Output "Задачи «$name» нет" }
        }
    }
    "status" {
        $missing = 0
        foreach ($name in $TaskName, $WatchTaskName) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if (-not $task) { Write-Output "Задачи «$name» нет"; $missing++; continue }
            $info = Get-ScheduledTaskInfo -TaskName $name
            Write-Output ("Задача «{0}»: {1}, последний запуск {2}, код {3}, следующий {4}" -f $name, $task.State,
                $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
        }
        if (Test-Path $OffFlag) { Write-Output "Пауза: есть data\AUTOSTART_OFF — движок сам не стартует и сторож молчит" }
        if (Test-Path $Log) { Write-Output "--- хвост $Log"; Get-Content $Log -Tail 5 }
        if ($missing) { exit 1 }
    }
    "run" {
        # Вызывается Планировщиком. Пишем в свой лог: engine.log ротирует engine.ps1 при старте
        if (Test-Path $OffFlag) { Write-AutostartLog "пропуск: есть data\AUTOSTART_OFF"; exit 0 }
        try {
            $out = & (Join-Path $PSScriptRoot "engine.ps1") start 2>&1 | Out-String
            Write-AutostartLog ("engine.ps1 start: " + ($out.Trim() -replace "\r?\n", " | "))
        }
        catch { Write-AutostartLog ("ошибка: " + $_.Exception.Message); exit 1 }
    }
    "watch" {
        # То же, что задача «OKX-Bot Watchdog», но с выводом в консоль. Решение и действия — в Python
        Push-Location $Root
        try { & $Py -m src.engine_watchdog --act; exit $LASTEXITCODE } finally { Pop-Location }
    }
}
