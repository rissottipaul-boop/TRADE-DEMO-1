<#
Контур — фоновый запуск локальной панели (src.control_panel). Windows PowerShell 5.1 и PowerShell 7.

    ops\panel.ps1 start [-Port 8765] [-Token <t>]  # запуск; успех — только после /health и проверки слушателя порта
    ops\panel.ps1 status                           # процесс этой копии + /health; код 0 — работает и отвечает
    ops\panel.ps1 stop                             # завершает только подтверждённые процессы этой копии
    ops\panel.ps1 restart | open | logs
    ops\panel.ps1 url [-ShowToken]                 # ссылка; токен целиком — только с явным -ShowToken

Безопасность (PANEL-LAUNCHER-SEC):
- data\control_panel.pid — JSON: PID лаунчера .venv\Scripts\python.exe и дочернего интерпретатора,
  время их создания, одноразовая метка запуска (nonce) в командной строке, порт. Процесс наш, только если
  совпали PID, время создания и метка, а у лаунчера ещё и путь к python этой копии. Повторно выданный PID,
  другая копия проекта, старый pid-файл с голым числом — предупреждение, процесс не трогаем.
- Порт служит только подтверждением: слушать его должен наш подтверждённый процесс. PID для остановки
  по порту не выбирается никогда.
- Токен новый на каждый запуск, на диске — только data\control_panel_token.dpapi под DPAPI текущего
  пользователя; src.control_panel_boot расшифровывает его сам, в командной строке токена нет. Скрипт его
  не печатает: url маскирует, полностью — только `url -ShowToken`; open передаёт ссылку браузеру.
- Запуск через ShellExecute: панель не наследует дескрипторы вызывающего и не держит его вывод.
  stdout сервера (там src.control_panel печатает ссылку с токеном) — в os.devnull, stderr — в
  logs\control-panel.log, на уровне дескрипторов 1/2. Непрочитанных пайпов нет, сервер не блокируется.
- В Windows PowerShell 5.1 из PSModulePath убираются каталоги модулей pwsh 7 (PANEL-LAUNCHER-PS51-ENV).
Тесты — tests/test_panel_launcher.py (обе версии PowerShell; 5.1 — и с PSModulePath потомка pwsh 7).
#>
param(
    [ValidateSet("start", "stop", "restart", "status", "url", "open", "logs")]
    [string]$Action = "status",
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$Token = "",
    [switch]$ShowToken,
    [ValidateRange(3, 300)][int]$StartTimeoutSec = 20,
    # Для тестов: интерпретатор другого окружения. По умолчанию — .venv этой копии.
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

function Repair-DesktopModulePath {
    # PANEL-LAUNCHER-PS51-ENV: Windows PowerShell 5.1, запущенный процессом с окружением pwsh 7 (например,
    # Python из терминала pwsh), наследует PSModulePath с каталогами модулей 7.x впереди. Автозагрузка
    # берёт оттуда Microsoft.PowerShell.Security, и ConvertTo-SecureString падает: «module could not be
    # loaded». pwsh, запуская powershell.exe сам, эти каталоги убирает; здесь делаем то же. Только 5.1.
    if ($PSVersionTable.PSEdition -ne "Desktop" -or -not $env:PSModulePath) { return }
    $kept = New-Object System.Collections.Generic.List[string]
    foreach ($entry in ($env:PSModulePath -split ';')) {
        $dir = $entry.Trim()
        if (-not $dir) { continue }
        # Documents\PowerShell\Modules, Program Files\PowerShell\Modules, ...\PowerShell\7\Modules;
        # \WindowsPowerShell\ под шаблон не попадает (перед PowerShell нет разделителя)
        if ($dir -match '(?i)\\PowerShell\\(\d[^\\]*\\)?Modules\\?$') { continue }
        if ($dir -match '(?i)\\WindowsApps\\Microsoft\.PowerShell') { continue }  # pwsh из Microsoft Store
        if (-not $kept.Contains($dir)) { $kept.Add($dir) }
    }
    $own = Join-Path $PSHOME "Modules"
    if (-not $kept.Contains($own)) { $kept.Add($own) }
    $env:PSModulePath = $kept -join ';'
}
Repair-DesktopModulePath

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $ProjectRoot "data\control_panel.pid"
$TokenFile = Join-Path $ProjectRoot "data\control_panel_token.dpapi"
$LegacyTokenFile = Join-Path $ProjectRoot "data\control_panel_token.txt"
$LogFile = Join-Path $ProjectRoot "logs\control-panel.log"
if (-not $PythonExe) { $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe" }
$PythonExe = [IO.Path]::GetFullPath($PythonExe)
$PortGiven = $PSBoundParameters.ContainsKey("Port")
$Marker = "kontur-panel"
$Utf8 = New-Object System.Text.UTF8Encoding $false

function Write-Info([string]$Text) { Write-Host $Text }

function Get-MaskedUrl([int]$P) { "http://127.0.0.1:$P/?token=<скрыт>" }

function Hide-Secrets([string]$Text) {
    # Защита при показе логов: старые версии лаунчера могли записать ссылку с токеном
    return ($Text -replace '(?i)(token=)[A-Za-z0-9_\-\.~%]+', '$1<скрыт>')
}

# ---------- процессы ----------

function Get-ProcInfo([int]$ProcessId) {
    if ($ProcessId -le 0) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Get-CreatedTicks($Cim) {
    if (-not $Cim -or -not $Cim.CreationDate) { return [int64]0 }
    return [int64]$Cim.CreationDate.ToUniversalTime().Ticks
}

function Test-OwnProcess($Cim, [string]$Ticks, [string]$Nonce, [switch]$CheckExe) {
    # Наш процесс = тот же момент создания (защита от повторно выданного PID)
    #             + метка этого запуска в командной строке (защита от копии проекта и чужого python)
    if (-not $Cim -or -not $Ticks -or -not $Nonce) { return $false }
    $recorded = [int64]0
    if (-not [int64]::TryParse($Ticks, [ref]$recorded) -or $recorded -le 0) { return $false }
    if ([math]::Abs((Get-CreatedTicks $Cim) - $recorded) -gt 10000000) { return $false }
    if (-not "$($Cim.CommandLine)".Contains("$Marker $Nonce")) { return $false }
    if ($CheckExe) {
        if (-not $Cim.ExecutablePath) { return $false }
        if (-not [string]::Equals([IO.Path]::GetFullPath($Cim.ExecutablePath), $PythonExe,
                [StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
    return $true
}

function Get-ListenerPids([int]$P) {
    # Только для подтверждения «слушает наш процесс»; выбирать по этому списку, что завершать, нельзя
    $found = @()
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $found = @(Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { [int]$_.OwningProcess })
    } else {
        foreach ($line in @(netstat -ano -p TCP)) {
            $cols = @("$line".Trim() -split '\s+')
            if ($cols.Count -ge 5 -and $cols[1] -match ":$P$" -and $cols[3] -match '^(LISTENING|ПРОСЛУШИВАНИЕ)$') {
                $found += [int]$cols[4]
            }
        }
    }
    return @($found | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
}

function Test-PanelHealth([int]$P) {
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$P/health")
        $req.Proxy = $null
        $req.Timeout = 2000
        $req.ReadWriteTimeout = 2000
        $resp = $req.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
            $body = $reader.ReadToEnd()
        } finally { $resp.Close() }
        return [bool](($body | ConvertFrom-Json).ok -eq $true)
    } catch { return $false }
}

# ---------- записи на диске ----------

function Read-Record {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = ([IO.File]::ReadAllText($PidFile)).Trim()
    $obj = $null
    try { $obj = $raw | ConvertFrom-Json } catch { $obj = $null }
    if ($obj -is [System.Management.Automation.PSCustomObject] -and $obj.nonce -and $obj.launcher_pid) { return $obj }
    return [PSCustomObject]@{ legacy = $true; raw = $raw }
}

function Save-Record($Obj) {
    [IO.File]::WriteAllText($PidFile, ($Obj | ConvertTo-Json -Compress), $Utf8)
}

function Save-Token([string]$Value) {
    $secure = ConvertTo-SecureString -String $Value -AsPlainText -Force
    [IO.File]::WriteAllText($TokenFile, (ConvertFrom-SecureString -SecureString $secure), $Utf8)
}

function Read-Token {
    if (-not (Test-Path -LiteralPath $TokenFile)) { return "" }
    try {
        $secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($TokenFile).Trim())
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    } catch { return "" }
}

function Remove-PanelFiles {
    foreach ($f in @($PidFile, $TokenFile)) {
        if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
    }
}

function Remove-LegacyToken {
    # Открытый токен прежней версии лаунчера: панель с ним уже не работает, хранить его незачем
    if (Test-Path -LiteralPath $LegacyTokenFile) {
        Remove-Item -LiteralPath $LegacyTokenFile -Force -ErrorAction SilentlyContinue
        Write-Info "Удалён устаревший файл с открытым токеном: data\control_panel_token.txt"
    }
}

function New-PanelToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($bytes) -replace '\+', '-' -replace '/', '_' -replace '=', '')
}

# ---------- состояние экземпляра ----------

function Get-PanelInstance {
    $state = [PSCustomObject]@{
        Record = $null; Legacy = $false; Port = $Port
        LauncherPid = 0; ServerPid = 0; Pids = @(); Problems = @()
    }
    $rec = Read-Record
    if (-not $rec) { return $state }
    $state.Record = $rec
    if ($rec.legacy) {
        $state.Legacy = $true
        $state.Problems += "data\control_panel.pid старого формата ('$($rec.raw)'): принадлежность процесса не подтвердить, его не трогаю"
        return $state
    }
    if ($rec.port) { $state.Port = [int]$rec.port }
    $launcher = Get-ProcInfo ([int]$rec.launcher_pid)
    if (Test-OwnProcess $launcher "$($rec.launcher_created)" "$($rec.nonce)" -CheckExe) {
        $state.LauncherPid = [int]$rec.launcher_pid
    } elseif ($launcher) {
        $state.Problems += "PID $($rec.launcher_pid) занят другим процессом (повторно выданный PID или чужая копия): не трогаю"
    }
    if ($rec.server_pid -and [int]$rec.server_pid -ne [int]$rec.launcher_pid) {
        $server = Get-ProcInfo ([int]$rec.server_pid)
        if (Test-OwnProcess $server "$($rec.server_created)" "$($rec.nonce)") {
            $state.ServerPid = [int]$rec.server_pid
        } elseif ($server) {
            $state.Problems += "PID $($rec.server_pid) занят другим процессом: не трогаю"
        }
    } elseif ($state.LauncherPid -and $rec.server_pid) {
        $state.ServerPid = $state.LauncherPid
    }
    $state.Pids = @(@($state.ServerPid, $state.LauncherPid) | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    return $state
}

function Test-PanelConfirmed($State) {
    # /health отвечает, и порт слушают только подтверждённые процессы этой копии
    if ($State.Pids.Count -eq 0) { return $false }
    $listeners = @(Get-ListenerPids $State.Port)
    if ($listeners.Count -eq 0) { return $false }
    foreach ($l in $listeners) { if ($State.Pids -notcontains $l) { return $false } }
    return (Test-PanelHealth $State.Port)
}

function Write-Problems($State) {
    foreach ($p in $State.Problems) { Write-Info "ВНИМАНИЕ: $p" }
}

function Stop-OwnProcesses($State) {
    # Завершаем только PID, подтверждённые прямо сейчас; сервер первым, лаунчер следом
    $fresh = Get-PanelInstance
    $targets = @($fresh.Pids | Where-Object { $State.Pids -contains $_ })
    foreach ($id in @($fresh.ServerPid, $fresh.LauncherPid)) {
        if ($id -gt 0 -and $targets -contains $id) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    }
    for ($i = 0; $i -lt 25; $i++) {
        $alive = @($targets | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return $false
}

# ---------- действия ----------

function Invoke-Stop {
    $state = Get-PanelInstance
    Write-Problems $state
    Remove-LegacyToken
    if ($state.Pids.Count -eq 0) {
        if ($state.Record) { Remove-PanelFiles; Write-Info "Панель не запущена; устаревшая запись удалена." }
        else { Write-Info "Панель не запущена." }
        $listeners = @(Get-ListenerPids $state.Port)
        if ($listeners.Count -gt 0) {
            Write-Info "Порт $($state.Port) слушает PID $($listeners -join ', ') — это не подтверждённая копия панели этого каталога, не трогаю."
        }
        return 0
    }
    Write-Info "Останавливаю панель: PID $($state.Pids -join ', ')"
    if (-not (Stop-OwnProcesses $state)) {
        Write-Info "Процессы панели не завершились за 5 с; запись оставлена для повторного stop."
        return 1
    }
    Remove-PanelFiles
    Write-Info "Панель остановлена."
    return 0
}

function Get-MutexName {
    # Имя от пути копии: разные копии проекта не ждут друг друга. GetHashCode в .NET 7 случаен в каждом процессе
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash($Utf8.GetBytes($ProjectRoot.ToLowerInvariant())) } finally { $sha.Dispose() }
    return "Local\OKX-Bot-panel-" + (($hash[0..7] | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Invoke-Start {
    # Мьютекс охватывает и проверку состояния, и запуск: два start не поднимут две панели
    $mutex = New-Object System.Threading.Mutex($false, (Get-MutexName))
    try { $owned = $mutex.WaitOne(60000) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { $mutex.Dispose(); Write-Info "Другой запуск панели идёт дольше минуты — пропускаю."; return 1 }
    try { return (Invoke-StartLocked) }
    finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
}

function Invoke-StartLocked {
    $state = Get-PanelInstance
    if ($state.Pids.Count -gt 0) {
        if (Test-PanelConfirmed $state) {
            Write-Info "Панель уже работает: PID $($state.Pids -join ', '), порт $($state.Port), /health ok"
            Write-Info "Ссылка: $(Get-MaskedUrl $state.Port) (ops\panel.ps1 open или url -ShowToken)"
            return 0
        }
        Write-Info "Процесс панели этой копии жив (PID $($state.Pids -join ', ')), но /health или слушатель порта $($state.Port) не подтверждены. Выполните ops\panel.ps1 restart."
        return 1
    }
    Write-Problems $state
    if ($state.Record) { Remove-PanelFiles }
    Remove-LegacyToken

    $p = $Port
    if (-not $PortGiven -and $state.Record -and -not $state.Legacy -and $state.Port) { $p = $state.Port }
    if (-not (Test-Path -LiteralPath $PythonExe)) { Write-Info "Нет интерпретатора: $PythonExe"; return 1 }
    $busy = @(Get-ListenerPids $p)
    if ($busy.Count -gt 0) {
        Write-Info "Порт $p уже слушает PID $($busy -join ', ') — это не подтверждённая копия панели этого каталога. Процесс не трогаю; выберите другой -Port."
        return 1
    }
    if ($Token) {
        if ($Token -notmatch '^[A-Za-z0-9_\-]{22,128}$') {
            Write-Info "Токен отклонён: нужны 22–128 символов [A-Za-z0-9_-]."
            return 1
        }
        $useToken = $Token
    } else { $useToken = New-PanelToken }
    $nonce = [guid]::NewGuid().ToString("N")

    foreach ($d in @((Split-Path -Parent $LogFile), (Split-Path -Parent $PidFile))) {
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
    # Токен — только в DPAPI-файле, его расшифровывает src.control_panel_boot. В командной строке —
    # метка запуска, порт и пути. ShellExecute не передаёт дочернему процессу наследуемые дескрипторы
    # вызывающего (его stdout/stderr): иначе фоновая панель держала бы чужой вывод до своей остановки.
    $proc = $null
    try {
        Save-Token $useToken
        $useToken = $null
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $PythonExe
        $psi.Arguments = "-m src.control_panel_boot $Marker $nonce $p `"$LogFile`" `"$TokenFile`""
        $psi.WorkingDirectory = $ProjectRoot
        $psi.UseShellExecute = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $proc = [System.Diagnostics.Process]::Start($psi)
        if (-not $proc) { throw "Process.Start не вернул процесс" }

        $launcherCim = Get-ProcInfo $proc.Id
        $record = [ordered]@{
            schema = 1; nonce = $nonce; port = $p; python = $PythonExe
            launcher_pid = $proc.Id; launcher_created = [string](Get-CreatedTicks $launcherCim)
            server_pid = 0; server_created = ""
            started_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
        }
        Save-Record ([PSCustomObject]$record)

        $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
        $serverPid = 0; $failure = ""
        while ((Get-Date) -lt $deadline) {
            if ($proc.HasExited) { $failure = "процесс завершился с кодом $($proc.ExitCode)"; break }
            # Сначала слушатели порта, потом свои процессы (PANEL-LAUNCHER-FLAKY). Лаунчер .venv\Scripts\python.exe
            # сам порт не слушает: сервер — его дочерний интерпретатор. При обратном порядке сервер, начавший
            # слушать между двумя запросами, выглядел посторонним, и start падал. Процесс, который уже слушает
            # порт, к следующему запросу точно виден в списке процессов.
            $listeners = @(Get-ListenerPids $p)
            $own = @($proc.Id)
            $own += @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)" -ErrorAction SilentlyContinue |
                Where-Object { "$($_.CommandLine)".Contains("$Marker $nonce") } | ForEach-Object { [int]$_.ProcessId })
            $foreign = @($listeners | Where-Object { $own -notcontains $_ })
            if ($foreign.Count -gt 0) { $failure = "порт $p слушает посторонний PID $($foreign -join ', ')"; break }
            if ($listeners.Count -gt 0 -and (Test-PanelHealth $p)) { $serverPid = [int]$listeners[0]; break }
            Start-Sleep -Milliseconds 300
        }
        if (-not $serverPid -and -not $failure) { $failure = "/health не ответил за $StartTimeoutSec с" }
        if ($failure) {
            Write-Info "Панель не запущена: $failure."
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)" -ErrorAction SilentlyContinue |
                Where-Object { "$($_.CommandLine)".Contains("$Marker $nonce") })
            foreach ($c in $children) { Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue }
            if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
            Remove-PanelFiles
            if (Test-Path -LiteralPath $LogFile) {
                Write-Info "--- хвост logs\control-panel.log"
                Get-Content -LiteralPath $LogFile -Tail 15 | ForEach-Object { Write-Info (Hide-Secrets $_) }
            }
            return 1
        }
        $serverCim = Get-ProcInfo $serverPid
        $record.server_pid = $serverPid
        $record.server_created = [string](Get-CreatedTicks $serverCim)
        Save-Record ([PSCustomObject]$record)
        Write-Info "Панель запущена: лаунчер PID $($proc.Id), сервер PID $serverPid, порт $p, /health ok"
        Write-Info "Ссылка: $(Get-MaskedUrl $p) — откройте ops\panel.ps1 open (полная ссылка: url -ShowToken)"
        return 0
    } catch {
        Write-Info "Ошибка запуска панели: $($_.Exception.Message)"
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
        Remove-PanelFiles
        return 1
    }
}

function Invoke-Status {
    $state = Get-PanelInstance
    Write-Problems $state
    $running = $state.Pids.Count -gt 0
    $healthy = $false
    if ($running) { $healthy = Test-PanelConfirmed $state }
    $listeners = @(Get-ListenerPids $state.Port)
    $note = ""
    if (-not $running -and $listeners.Count -gt 0) {
        $note = "порт слушает PID $($listeners -join ', ') — не подтверждённая копия панели этого каталога"
    }
    $url = ""
    if ($running) { $url = Get-MaskedUrl $state.Port }
    $out = [PSCustomObject]@{
        PanelName   = "Kontur Control Panel"
        Running     = $running
        LauncherPid = $state.LauncherPid
        ServerPid   = $state.ServerPid
        Port        = $state.Port
        ApiHealthy  = $healthy
        DashboardUrl = $url
        Note        = $note
    } | Format-List | Out-String -Width 220
    Write-Info $out.TrimEnd()
    if ($running -and $healthy) { return 0 }
    return 1
}

function Get-PanelLink {
    # Полная ссылка только для url -ShowToken и open; в остальных выводах — Get-MaskedUrl
    $state = Get-PanelInstance
    if (-not (Test-PanelConfirmed $state)) { Write-Info "Панель этой копии не запущена или не отвечает: ops\panel.ps1 start"; return $null }
    $t = Read-Token
    if (-not $t) { Write-Info "Токен недоступен (data\control_panel_token.dpapi): ops\panel.ps1 restart"; return $null }
    return [PSCustomObject]@{ Port = $state.Port; Full = "http://127.0.0.1:$($state.Port)/?token=$t" }
}

switch ($Action) {
    "start" { exit (Invoke-Start) }
    "stop" { exit (Invoke-Stop) }
    "restart" {
        $rc = Invoke-Stop
        if ($rc -ne 0) { exit $rc }
        exit (Invoke-Start)
    }
    "status" { exit (Invoke-Status) }
    "url" {
        $link = Get-PanelLink
        if (-not $link) { exit 1 }
        if ($ShowToken) { Write-Output $link.Full }
        else { Write-Output ((Get-MaskedUrl $link.Port) + "   (полная ссылка: -ShowToken)") }
        exit 0
    }
    "open" {
        if (-not (Test-PanelConfirmed (Get-PanelInstance))) {
            $rc = Invoke-Start
            if ($rc -ne 0) { exit $rc }
        }
        $link = Get-PanelLink
        if (-not $link) { exit 1 }
        Start-Process $link.Full
        Write-Info "Панель открыта в браузере: $(Get-MaskedUrl $link.Port)"
        exit 0
    }
    "logs" {
        if (Test-Path -LiteralPath $LogFile) {
            Get-Content -LiteralPath $LogFile -Tail 50 | ForEach-Object { Write-Output (Hide-Secrets $_) }
        } else { Write-Info "Лога нет: $LogFile" }
        exit 0
    }
}
