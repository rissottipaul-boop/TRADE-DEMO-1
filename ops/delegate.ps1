<#
Мост делегирования: Claude / Antigravity-Gemini / Codex отдают подзадачи исполнителям Muse.

    ops\delegate.ps1 submit -Prompt "..." [-From claude] [-Role insight-executor] [-TimeoutMin 20]
    ops\delegate.ps1 fetch [-Id <id>]
    ops\delegate.ps1 run-once          # разобрать всю очередь сейчас (один проход)
    ops\delegate.ps1 start            # persistent runner: цикл раз в 30 с (pid в ops\delegations\runner.pid)
    ops\delegate.ps1 stop             # штатная остановка раннера
    ops\delegate.ps1 status           # раннер + глубины очередей + хвост лога
    ops\delegate.ps1 check            # доступен ли muse для исполнения заявок
    ops\delegate.ps1 register         # автозапуск раннера при входе (выполняет человек)
    ops\delegate.ps1 unregister      # убрать автозапуск

Очередь — файлы ops\delegations\{inbox,processing,outbox,done}\. Прямая запись заявки
в inbox (без submit) разрешена: схема — ops\delegations\README.md.
Исполнитель — `muse exec` (порядок: $env:DELEGATE_MUSE_CMD, иначе ops\muse.cmd через WSL,
иначе muse из PATH). Каждая заявка дополняется шапкой: соблюдать AGENTS.md, действия
правой колонки §2 запрещены. Политика full access — решение человека 30.09.2026.
Пауза без остановки процесса — файл ops\delegations\PAUSED. Лог — logs\delegation.log.
Скрипт намеренно без .NET-вызовов: работает и в ConstrainedLanguage, и в 5.1, и в pwsh 7.
#>
param(
    [ValidateSet("submit", "fetch", "run-once", "loop", "start", "stop", "status", "check", "register", "unregister")]
    [string]$Action = "status",
    [string]$Prompt = "",
    [string]$From = "unknown",
    [string]$Role = "",
    [int]$TimeoutMin = 20,
    [string]$Id = "",
    [int]$ForceTimeoutSec = 0,
    [int]$PollSec = 30,
    [string]$Model = "muse-spark-1.3"
)

$ErrorActionPreference = "Stop"
# UNC-префикс \\?\ ломает cmd-лаунчеры (muse.cmd через WSL): чистим корень сразу
$Root = (Split-Path -Parent $PSScriptRoot) -replace '^\\\\\?\\', ''
$Deleg = Join-Path $Root "ops\delegations"
$Inbox = Join-Path $Deleg "inbox"
$Processing = Join-Path $Deleg "processing"
$Outbox = Join-Path $Deleg "outbox"
$DoneDir = Join-Path $Deleg "done"
$PidFile = Join-Path $Deleg "runner.pid"
$StopFlag = Join-Path $Deleg "STOP"
$PauseFlag = Join-Path $Deleg "PAUSED"
$Log = Join-Path $Root "logs\delegation.log"
$TaskName = "OKX-Bot Delegation"

New-Item -ItemType Directory -Force $Inbox, $Processing, $Outbox, $DoneDir, (Split-Path $Log) | Out-Null

function Write-DelegLog([string]$Text) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"), $Text
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

function Resolve-MuseCommand {
    if ($env:DELEGATE_MUSE_CMD -and $env:DELEGATE_MUSE_CMD.Trim()) {
        return $env:DELEGATE_MUSE_CMD.Trim() -replace '^\\\\\?\\', ''
    }
    $local = Join-Path $Root "ops\muse.cmd"
    if (Test-Path $local) { return $local }
    $found = Get-Command muse -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

function Get-RunnerProcess {
    # Жив ли раннер ЭТОГО каталога: pid-файл хранит "PID|Ticks старта" — защита от
    # повторно выданного PID без CIM/CommandLine (работает и в ConstrainedLanguage)
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = "$(Get-Content $PidFile -Raw -Encoding UTF8)".Trim()
    $parts = $raw -split "\|"
    if ($parts.Count -ne 2 -or $parts[0] -notmatch '^\d+$' -or $parts[1] -notmatch '^\d+$') { return $null }
    $proc = Get-Process -Id $parts[0] -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    try { $ticks = $proc.StartTime.Ticks } catch { return $null }
    if ("$ticks" -eq $parts[1]) { return $proc }
    return $null
}

function Wait-ProcSeconds($Proc, [int]$Seconds) {
    # Ожидание с опросом раз в секунду: WaitForExit(ms) недоступен в ConstrainedLanguage
    $waited = 0
    while (-not $Proc.HasExited -and $waited -lt $Seconds) {
        Start-Sleep -Seconds 1
        $waited++
    }
    return $Proc.HasExited
}

function Invoke-OneRequest([string]$ReqPath, [string]$MuseCmd) {
    $name = ($ReqPath -split '[\\/]')[-1] -replace '\.[^.]*$', ''
    $req = $null
    try { $req = Get-Content $ReqPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { $req = $null }
    $failReason = $null
    if (-not $req) { $failReason = "bad json: заявка не парсится" }
    elseif (-not ($req.prompt -is [string]) -or -not $req.prompt.Trim()) { $failReason = "bad request: пустой prompt" }
    if ($failReason) {
        $box = @{ id = $name; status = "error"; error = $failReason;
            finished = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz") }
        Set-Content -Path (Join-Path $Outbox "$name.json") -Value ($box | ConvertTo-Json -Depth 5) -Encoding UTF8
        Move-Item $ReqPath (Join-Path $DoneDir "$name.json") -Force
        Write-DelegLog "$name error ($failReason), muse не вызывался"
        return
    }
    $claim = Join-Path $Processing "$name.json"
    try { Move-Item $ReqPath $claim -Force }
    catch { Write-DelegLog "$name пропущен: уже забран"; return }
    $timeoutSec = 1200
    if (($req.timeout_min -is [int] -or $req.timeout_min -is [long]) -and $req.timeout_min -ge 1) {
        $timeoutSec = [int]$req.timeout_min * 60
        if ($timeoutSec -gt 7200) { $timeoutSec = 7200 }
    }
    if ($ForceTimeoutSec -gt 0) { $timeoutSec = $ForceTimeoutSec }
    $promptFile = Join-Path $Processing "$name.prompt.md"
    $header = @(
        "Ты — исполнитель Muse в проекте OKX-бота (корень: $Root).",
        "Прочитай AGENTS.md и соблюдай его буквально, включая §2: действия правой колонки",
        "(live-торговля, сброс kill-switch/breaker, ослабление лимитов, секреты, удаление",
        "данных, правка guard/автопилота, вывод средств) ЗАПРЕЩЕНЫ.",
        "Работай только в пределах заявки ниже. Итог выведи в stdout: что сделано,",
        "изменённые файлы, команды проверок и их результат.",
        "---",
        "ЗАЯВКА $name от $($req.from) (роль: $($req.role)):"
        $req.prompt
    ) -join "`r`n"
    Set-Content -Path $promptFile -Value $header -Encoding UTF8
    $status = "done"; $exitCode = $null; $errText = ""; $elapsed = 0; $killed = $true
    $tmpOut = Join-Path $Processing "$name.stdout.txt"
    $tmpErr = Join-Path $Processing "$name.stderr.txt"
    $codeFile = Join-Path $Processing "$name.exitcode.txt"
    if (-not $MuseCmd) {
        $status = "error"; $errText = "muse not found: нет DELEGATE_MUSE_CMD, ops\muse.cmd и muse в PATH"
    }
    else {
        # Одна строка аргументов с кавычками: массив -ArgumentList в 5.1 склеивается
        # через пробел без квотирования и ломает пути с пробелами ("TRADE DEMO 1")
        # Путь относительно корня: muse.cmd запускает muse в WSL (--cd корень), где
        # Windows-путь C:\... не читается; cwd обоих вариантов — $Root
        $promptRel = "ops/delegations/processing/$name.prompt.md"
        $argList = 'exec --model "{0}" --prompt-file "{1}"' -f $Model, $promptRel
        if ($env:DELEGATE_MUSE_ARGS -and $env:DELEGATE_MUSE_ARGS.Trim()) {
            $argList += " " + $env:DELEGATE_MUSE_ARGS.Trim()
        }
        try {
            # Код выхода — через файл: $proc.ExitCode под ограниченным токеном
            # песочницы отдаёт $null даже после HasExited. Вложенный cmd: `exit`
            # внутри .cmd убивает только внутренний интерпретатор, внешний пишет код.
            $inner = '""{0}" {1}"' -f $MuseCmd, $argList
            $wrapper = '/v:on /c "cmd /c {0} & echo !ERRORLEVEL! > "{1}""' -f $inner, $codeFile
            $proc = Start-Process -FilePath "cmd" -ArgumentList $wrapper -WorkingDirectory $Root `
                -NoNewWindow -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr -PassThru
            $waited = 0
            while (-not $proc.HasExited -and $waited -lt $timeoutSec) {
                Start-Sleep -Seconds 1
                $waited++
            }
            $elapsed = $waited
            if ($proc.HasExited) {
                $raw = ""
                try { $raw = (Get-Content $codeFile -Raw -ErrorAction Stop).Trim() } catch { $raw = "" }
                if ($raw -match '^-?\d+$') { $exitCode = [int]$raw }
                else { $status = "error"; $errText = "нет кода выхода muse" }
            }
            else {
                $status = "timeout"; $errText = "превышен лимит $timeoutSec с"
                # try/catch обязателен: taskkill под Stop бросает NativeCommandError
                # даже с >$null 2>$null (проверено в 5.1); исход — во флаге killed
                try { taskkill /F /T /PID $proc.Id >$null 2>$null } catch { }
                $killed = Wait-ProcSeconds $proc 3
                if (-not $killed) { $errText += "; процесс не завершён после kill" }
            }
        }
        catch { $status = "error"; $errText = "запуск muse: $($_.Exception.Message)" }
        if ($status -eq "done" -and $exitCode -ne 0) { $status = "error"; $errText = "muse exit $exitCode" }
    }
    $fullOut = ""
    if (Test-Path $tmpOut) {
        $fullOut += "[stdout]`r`n" + (Get-Content $tmpOut -Raw -Encoding UTF8) + "`r`n"
        Remove-Item $tmpOut -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $tmpErr) {
        $txt = Get-Content $tmpErr -Raw -Encoding UTF8
        if ($txt -and $txt.Trim()) { $fullOut += "[stderr]`r`n$txt`r`n" }
        Remove-Item $tmpErr -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $codeFile -Force -ErrorAction SilentlyContinue
    $tail = "$fullOut"
    if ($tail.Length -gt 4000) { $tail = "<обрезано> ... " + $tail.Substring($tail.Length - 4000) }
    $box = @{ id = $name; status = $status; exit_code = $exitCode; elapsed_s = $elapsed;
        killed = $killed; finished = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz");
        error = $errText; output_tail = $tail }
    Set-Content -Path (Join-Path $Outbox "$name.json") -Value ($box | ConvertTo-Json -Depth 5) -Encoding UTF8
    Set-Content -Path (Join-Path $Outbox "$name.log") -Value $fullOut -Encoding UTF8
    Move-Item $promptFile (Join-Path $Outbox "$name.prompt.md") -Force -ErrorAction SilentlyContinue
    Move-Item $claim (Join-Path $DoneDir "$name.json") -Force
    Write-DelegLog "$name $status exit=$exitCode за $elapsed с $errText"
}

function Invoke-RunOnce([string]$MuseCmd) {
    if (Test-Path $PauseFlag) { Write-Output "Пауза: есть ops\delegations\PAUSED"; return }
    $files = @(Get-ChildItem $Inbox -Filter "*.json" -ErrorAction SilentlyContinue | Sort-Object Name)
    if (-not $files.Count) { Write-Output "Очередь пуста"; return }
    Write-Output "Заявок: $($files.Count)"
    foreach ($f in $files) { Invoke-OneRequest $f.FullName $MuseCmd }
}

switch ($Action) {
    "submit" {
        if (-not $Prompt.Trim()) { throw "submit: нужен -Prompt" }
        if ($TimeoutMin -lt 1 -or $TimeoutMin -gt 120) { throw "submit: -TimeoutMin 1..120" }
        $id = "d" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + (Get-Random -Maximum 65535).ToString("X4")
        $req = @{ id = $id; from = $From; role = $Role; prompt = $Prompt;
            timeout_min = $TimeoutMin; created = (Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz") }
        Set-Content -Path (Join-Path $Inbox "$id.json") -Value ($req | ConvertTo-Json -Depth 5) -Encoding UTF8
        Write-DelegLog "$id queued от $From (роль: $Role)"
        Write-Output $id
    }
    "fetch" {
        if (-not $Id) {
            $files = @(Get-ChildItem $Outbox -Filter "d*.json" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | Select-Object -First 10)
            if (-not $files.Count) { Write-Output "Результатов нет"; break }
            foreach ($f in $files) {
                try { $b = Get-Content $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json }
                catch { continue }
                Write-Output ("{0} {1} exit={2} {3}" -f $b.id, $b.status, $b.exit_code, $b.finished)
            }
            break
        }
        $box = Join-Path $Outbox "$Id.json"
        if (Test-Path $box) { Get-Content $box -Raw -Encoding UTF8; break }
        if ((Test-Path (Join-Path $Inbox "$Id.json")) -or (Test-Path (Join-Path $Processing "$Id.json"))) {
            Write-Output "pending"; break
        }
        Write-Output "unknown id: $Id"; exit 1
    }
    "run-once" {
        Invoke-RunOnce (Resolve-MuseCommand)
    }
    "loop" {
        Write-DelegLog "цикл запущен (опрос $PollSec с)"
        while (-not (Test-Path $StopFlag)) {
            try { Invoke-RunOnce (Resolve-MuseCommand) | Out-Null }
            catch { Write-DelegLog ("ошибка прохода: " + $_.Exception.Message) }
            Start-Sleep -Seconds $PollSec
        }
        Write-DelegLog "цикл остановлен по флагу STOP"
    }
    "start" {
        # Двойной старт безопасен и без мьютекса: заявку забирает атомарный Move-Item,
        # второй цикл увидит пустую очередь. Проверка pid — от случайных дублей.
        $running = Get-RunnerProcess
        if ($running) { Write-Output "Раннер уже работает: PID $($running.Id)"; exit 0 }
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
        if (Test-Path $StopFlag) { Remove-Item $StopFlag -Force }
        if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 2MB) {
            $archived = Join-Path $Root ("logs\delegation_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
            Move-Item $Log $archived
            Write-Output "Предыдущий лог сохранён: $archived"
        }
        $shell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
        if ($pwsh) { $shell = $pwsh.Source }
        $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" loop' -f $PSCommandPath
        $child = Start-Process -FilePath $shell -ArgumentList $arg -WorkingDirectory $Root `
            -WindowStyle Hidden -PassThru
        Set-Content -Path $PidFile -Value ("{0}|{1}" -f $child.Id, $child.StartTime.Ticks) -Encoding UTF8
        Write-DelegLog "раннер запущен: PID $($child.Id)"
        Write-Output "Раннер запущен: PID $($child.Id)"
    }
    "stop" {
        $running = Get-RunnerProcess
        if (-not $running) {
            if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
            Write-Output "Раннер не запущен"; break
        }
        Set-Content -Path $StopFlag -Value "" -Encoding UTF8
        $waited = 0
        while ($waited -lt 45 -and (Get-RunnerProcess)) { Start-Sleep -Seconds 1; $waited++ }
        if (Get-RunnerProcess) {
            Stop-Process -Id $running.Id -Force
            Write-Output "Раннер не остановился за 45 с — завершён принудительно"
        }
        else { Write-Output "Раннер остановлен" }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item $StopFlag -Force -ErrorAction SilentlyContinue
        Write-DelegLog "раннер остановлен"
    }
    "status" {
        $running = Get-RunnerProcess
        if ($running) { Write-Output "Раннер: работает, PID $($running.Id)" }
        else { Write-Output "Раннер: остановлен" }
        $qi = @(Get-ChildItem $Inbox -Filter "*.json" -ErrorAction SilentlyContinue).Count
        $qp = @(Get-ChildItem $Processing -Filter "d*.json" -ErrorAction SilentlyContinue).Count
        $qo = @(Get-ChildItem $Outbox -Filter "d*.json" -ErrorAction SilentlyContinue).Count
        Write-Output "Очередь: inbox $qi, processing $qp, outbox $qo"
        if (Test-Path $PauseFlag) { Write-Output "Пауза: есть ops\delegations\PAUSED" }
        if (Test-Path $Log) { Write-Output "--- хвост $Log"; Get-Content $Log -Tail 8 }
    }
    "check" {
        $muse = Resolve-MuseCommand
        if (-not $muse) {
            Write-Output "FAIL: muse не найден (DELEGATE_MUSE_CMD, ops\muse.cmd, PATH)"
            exit 1
        }
        Write-Output "muse: $muse"
        $tmpOut = Join-Path $Processing "_check.stdout.txt"
        $tmpErr = Join-Path $Processing "_check.stderr.txt"
        $codeFile = Join-Path $Processing "_check.exitcode.txt"
        try {
            $inner = '""{0}" exec --help"' -f $muse
            $wrapper = '/v:on /c "cmd /c {0} & echo !ERRORLEVEL! > "{1}""' -f $inner, $codeFile
            $proc = Start-Process -FilePath "cmd" -ArgumentList $wrapper `
                -WorkingDirectory $Root -NoNewWindow `
                -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr -PassThru
            # Холодный старт WSL занимает десятки секунд — ждём до 2 мин
            if (-not (Wait-ProcSeconds $proc 120)) {
                try { taskkill /F /T /PID $proc.Id >$null 2>$null } catch { }
                Write-Output "FAIL: muse exec --help не ответил за 120 с"; exit 1
            }
            $help = ""
            if (Test-Path $tmpOut) { $help = Get-Content $tmpOut -Raw -Encoding UTF8 }
            $code = ""
            try { $code = (Get-Content $codeFile -Raw -ErrorAction Stop).Trim() } catch { $code = "" }
            if ($code -ne "0") {
                Write-Output "FAIL: muse exec --help exit $code"; exit 1
            }
            if ($help -notmatch "--model" -or $help -notmatch "prompt-file") {
                Write-Output "FAIL: exec --help без --model/--prompt-file — проверь версию muse"; exit 1
            }
            Write-Output "OK: muse exec доступен, --model/--prompt-file на месте"
        }
        finally {
            Remove-Item $tmpOut, $tmpErr, $codeFile -Force -ErrorAction SilentlyContinue
        }
    }
    "register" {
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $shell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" start' -f $PSCommandPath
        $taskAction = New-ScheduledTaskAction -Execute $shell -Argument $arg -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $trigger.Delay = "PT2M"
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "Мост делегирования: ops\delegate.ps1 start при входе (DELEG-MUSE-QUEUE)" -Force | Out-Null
        Write-Output "Задача «$TaskName» зарегистрирована: вход пользователя + 2 мин → ops\delegate.ps1 start"
    }
    "unregister" {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Output "Задача «$TaskName» удалена"
        }
        else { Write-Output "Задачи «$TaskName» нет" }
    }
}
