<#
Планировщик памп-сканера Windows (PUMP-SCHED).
Ежечасный запуск детерминированного сканера рынка без LLM.

    ops\pump_scan.ps1 run          # запуск скана (вызывает Планировщик)
    ops\pump_scan.ps1 status       # статус задачи в Планировщике
    ops\pump_scan.ps1 register     # зарегистрировать задачу в Планировщике
    ops\pump_scan.ps1 unregister   # удалить задачу из Планировщика
    ops\pump_scan.ps1 check        # сухой прогон передачи кандидата: без скана, журнала и доски

Пауза без удаления задачи — файл data\AUTOSTART_OFF.
Лог выполнения — logs\pump_scan.log.
При коде выхода сканера != 0 в data\pump_journal.jsonl записывается строка ошибки v=1.
После успешного скана — src.pump_handoff (PUMP-SCHED-HANDOFF): свежий кандидат из строки scan
переводит PUMP-SCAN на доске в ready с пометкой пары. Ордеров нет; ошибка передачи — код 3
(в журнал кармана не пишется, причина — в логе).
#>
param([ValidateSet("register", "unregister", "status", "run", "check")][string]$Action = "run")

$ErrorActionPreference = "Stop"
# Python пишет UTF-8, а pwsh декодирует вывод для *>> по OEM-кодировке консоли (866) —
# кракозябры в logs\pump_scan.log. Сбой установки не должен ронять скан.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = "OKX-Bot Pump-Scanner"
$OffFlag = Join-Path $Root "data\AUTOSTART_OFF"
$Log = Join-Path $Root "logs\pump_scan.log"
$Journal = Join-Path $Root "data\pump_journal.jsonl"
$Py = Join-Path $Root ".venv\Scripts\python.exe"

function Write-PumpLog([string]$Text) {
    $dir = Split-Path $Log
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"), $Text) -Encoding utf8
}

function Write-PumpJournalError([string]$Reason, [int]$Code = 2) {
    $dir = Split-Path $Journal
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $iso = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    $errObj = [ordered]@{
        ts = $iso
        event = "error"
        v = 1
        kind = "scan"
        code = $Code
        source = "ops/pump_scan.ps1"
        reason = $Reason
    }
    $json = ConvertTo-Json -InputObject $errObj -Compress
    Add-Content -Path $Journal -Value $json -Encoding utf8
}

switch ($Action) {
    "run" {
        if (Test-Path $OffFlag) {
            Write-PumpLog "пропуск: есть data\AUTOSTART_OFF"
            exit 0
        }

        if (-not (Test-Path $Py)) {
            Write-PumpLog "ошибка: нет $Py"
            Write-PumpJournalError "нет .venv\Scripts\python.exe в $Root" 2
            exit 2
        }

        Write-PumpLog "старт: запуск src.pump_scanner"
        & $Py -m src.pump_scanner --exclude ETH SOL SUI ADA TRX ETC APT BNB XLM DOT OKB *>> $Log
        $exitCode = $LASTEXITCODE

        if ($exitCode -eq 0) {
            Write-PumpLog "успешно: скан завершён (код 0)"
            # PUMP-SCHED-HANDOFF: свежий кандидат -> PUMP-SCAN ready на доске (без ордеров)
            & $Py -m src.pump_handoff *>> $Log
            $handoffCode = $LASTEXITCODE
            if ($handoffCode -ne 0) {
                Write-PumpLog "ошибка: передача кандидата (src.pump_handoff) завершилась с кодом $handoffCode"
                exit 3
            }
            exit 0
        } else {
            Write-PumpLog "ошибка: скан завершился с кодом $exitCode"
            Write-PumpJournalError "src.pump_scanner завершился с кодом $exitCode" $exitCode
            exit $exitCode
        }
    }
    "check" {
        # Сухой прогон передачи: скан не запускается, журнал и доска не меняются
        if (-not (Test-Path $Py)) { Write-Output "нет $Py"; exit 2 }
        & $Py -m src.pump_handoff --dry-run
        exit $LASTEXITCODE
    }
    "register" {
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

        $pwsh = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pwsh.exe"
        if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh -ErrorAction Stop).Source }
        $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" run' -f $PSCommandPath
        $taskAction = New-ScheduledTaskAction -Execute $pwsh -Argument $arg -WorkingDirectory $Root

        # Запуск ежечасно через 2 мин после закрытия свечи (:02:00)
        $start = (Get-Date).Date.AddHours((Get-Date).Hour).AddMinutes(2)
        if ($start -lt (Get-Date)) { $start = $start.AddHours(1) }
        $trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "Ежечасный памп-скан: ops\pump_scan.ps1 run (PUMP-SCHED)" -Force | Out-Null
        Write-Output "Задача «$TaskName» зарегистрирована: ежечасно в :02 -> ops\pump_scan.ps1 run"
    }
    "unregister" {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Output "Задача «$TaskName» удалена"
        } else {
            Write-Output "Задачи «$TaskName» нет"
        }
    }
    "status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Output "Задача «$TaskName»: не зарегистрирована"
            exit 1
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Output ("Задача «{0}»: {1}, последний запуск {2} (код {3}), следующий {4}" -f `
            $TaskName, $task.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
        exit 0
    }
}
