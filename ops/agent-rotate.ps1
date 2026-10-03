<#
.SYNOPSIS
    Agent launcher; role/model priorities: ops/agent-routing.json.
.DESCRIPTION
    -Plan prints JSON without model calls or logs. Missing CLIs are skipped.
    A started prompt is NEVER replayed automatically. Record a handoff on the
    board, then use -SkipAgent for an exhausted provider.
    Antigravity remains an alias for Gemini CLI; agy is a separate client.
    Autonomy is data-driven: when a runtime's guard_status in
    ops/agent-routing.json is 'e2e-verified' (per ops/hooks/launch-e2e.json),
    the launcher drops the analysis-only prefix and read-only sandbox and runs
    the client in full-auto mode. The project guard hook stays mandatory and
    still denies the protected actions from AGENTS.md §2.
.EXAMPLE
    ops\agent-rotate.ps1 -Role insight-executor -Plan
    ops\agent-rotate.ps1 -Agent codex -Role insight-executor -Prompt "Review T42"
    ops\agent-rotate.ps1 -Role insight-executor -SkipAgent codex -Plan
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'claude', 'antigravity', 'gemini', 'muse', 'codex')]
    [string]$Agent = 'auto',
    [ValidateSet('project-orchestrator', 'insight-executor', 'crypto-insight-hunter',
                 'okx-trader', 'pump-risk-taker', 'ops-sentinel')]
    [string]$Role,
    [string]$Prompt = 'Прочитай AGENTS.md и ops/board.md. Возьми следующую готовую задачу в своей роли, выполни, проверь и обнови доску.',
    [string]$TaskId,
    [ValidateSet('claude', 'antigravity', 'gemini', 'muse', 'codex')]
    [string[]]$SkipAgent = @(),
    [string]$Model,
    [string]$RunId,
    [switch]$Headless,
    [switch]$Plan
)
$ErrorActionPreference = 'Stop'
# Panel correlation ID only: logged verbatim, never added to the prompt.
if ($RunId -and $RunId -notmatch '^run_[A-Za-z0-9_-]{1,80}$') { throw 'Invalid -RunId format.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
$registry = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'agent-routing.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($registry.schema_version -ne 1) { throw 'Unsupported agent-routing schema.' }
if ($Model -and $Agent -eq 'auto') { throw '-Model requires an explicit -Agent.' }
if ($Agent -eq 'antigravity') { $Agent = 'gemini' }
$excluded = @($SkipAgent | ForEach-Object { if ($_ -eq 'antigravity') { 'gemini' } else { $_ } })
$roleConfig = if ($Role) { $registry.roles.$Role } else { $null }
$order = if ($Agent -ne 'auto') { @($Agent) }
         elseif ($Role) { @($roleConfig.priority) }
         else { @($registry.legacy_order) }
$context = 'Работай по AGENTS.md, ops/agent-team.md и единой ops/board.md. '
$context += 'Сначала SYNC; не забирай чужой активный claim. Перед передачей работы сохрани в заметках задачи файлы, проверки, незавершённые действия и следующий шаг. '
if ($Role) { $context += "Роль: $Role. Прочитай $($roleConfig.file). " }
$context += 'Экономь контекст по ops/token-economy.md, не снижая модель, проверки и риск-контроль. '
if ($TaskId) {
    $context += "Работай только над задачей $TaskId; после проверки остановись. "
    $context += "Карточку читай адресно: .venv/Scripts/python.exe -m src.agent_context --task $TaskId; при ошибке — исходную доску. "
}
$candidates = @()
foreach ($runtimeName in $order) {
    $runtime = $registry.runtimes.$runtimeName
    if (-not $runtime) { throw "Unknown runtime: $runtimeName" }
    $commandInfo = Get-Command $runtime.command -ErrorAction SilentlyContinue | Select-Object -First 1
    $commandPath = if ($commandInfo) { $commandInfo.Name } else { $null }
    $wrapper = $false
    if (-not $commandPath -and $runtimeName -eq 'muse') {
        $wrapperPath = Join-Path $PSScriptRoot 'muse.cmd'
        if (Test-Path -LiteralPath $wrapperPath) { $commandPath = $wrapperPath; $wrapper = $true }
    }
    $selectedModel = if ($Model) { $Model }
                     elseif ($Role) { $roleConfig.models.$runtimeName }
                     elseif ($runtimeName -eq 'codex') { $runtime.default_model }
                     else { $null }
    $guardVerified = ($runtime.guard_status -eq 'e2e-verified')
    $taskPrompt = $context + $Prompt
    if (-not $guardVerified -and ($runtimeName -eq 'codex' -or ($Role -and $runtimeName -in @('gemini', 'muse')))) {
        $taskPrompt = 'Для этого запуска разрешены только анализ, чтение и рекомендации: интеграция guard ещё не проверена. Не выполняй торговые и другие изменяющие внешнее состояние действия. ' + $taskPrompt
    }
    $cliArgs = @()
    switch ($runtimeName) {
        'claude' {
            if ($Headless) { $cliArgs += @('--print', '--output-format', 'text') }
            if ($selectedModel) { $cliArgs += @('--model', $selectedModel) }
            $cliArgs += @('--', $taskPrompt)
        }
        'gemini' {
            if ($guardVerified) { $cliArgs += @('--approval-mode', 'yolo') }
            elseif ($Role) { $cliArgs += @('--approval-mode', 'plan') }
            if ($selectedModel) { $cliArgs += @('--model', $selectedModel) }
            if ($Headless) { $cliArgs += @('--prompt', $taskPrompt) }
            else { $cliArgs += @('--prompt-interactive', $taskPrompt) }
        }
        'muse' {
            if ($Headless) { $cliArgs += 'exec' }
            if ($guardVerified) { $cliArgs += '--trust-workspace' }
            if ($selectedModel) { $cliArgs += @('--model', $selectedModel) }
            $cliArgs += @('--', $taskPrompt)
        }
        'codex' {
            $cliArgs += @('--ask-for-approval', 'never')
            if ($Headless) { $cliArgs += 'exec' }
            if ($guardVerified) {
                $cliArgs += @('--sandbox', 'workspace-write', '-c', 'sandbox_workspace_write.network_access=true')
            } else {
                $cliArgs += @('--sandbox', 'read-only')
            }
            $cliArgs += @('--cd', $projectRoot, '--model', $selectedModel)
            $cliArgs += @('--', $taskPrompt)
        }
    }
    $candidates += [pscustomobject]@{
        agent = $runtimeName; model = $selectedModel; command = $commandPath
        available = [bool]$commandPath; wrapper_unverified = $wrapper
        skipped = ($runtimeName -in $excluded); guard_status = $runtime.guard_status
        arguments = $cliArgs
    }
}
$selected = $candidates | Where-Object { $_.available -and -not $_.skipped } | Select-Object -First 1
if ($Plan) {
    [pscustomobject]@{
        role = $Role; task = $TaskId
        selected = $(if ($selected) { $selected.agent } else { $null })
        candidates = $candidates; retry_after_start = $false
    } | ConvertTo-Json -Depth 8
    exit 0
}
if (-not $selected) {
    Write-Error 'No eligible CLI found. Use -Plan to inspect candidates; agy is a separate client.'
    exit 1
}
# No prompt text or model output in the shared rotation log.
function Write-RotationLog([string]$Event, [int]$Code) {
    try {
        $logDir = Join-Path $projectRoot 'logs'
        $null = New-Item -ItemType Directory -Force -Path $logDir
        [ordered]@{ ts = (Get-Date -Format o); agent = $selected.agent; model = $selected.model
            role = $Role; task = $TaskId; event = $Event; exit_code = $Code
            run_id = $(if ($RunId) { $RunId } else { $null }) } |
            ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $logDir 'agent-rotate.log') -Encoding UTF8
    } catch { Write-Warning 'Could not write the rotation log.' }
}
Write-RotationLog 'start' 0
Write-Host "Agent: $($selected.agent); model: $($selected.model); guard: $($selected.guard_status)"
Push-Location $projectRoot
$resultCode = 1
try {
    $global:LASTEXITCODE = 0
    $runArgs = @($selected.arguments)
    & $selected.command @runArgs
    $resultCode = $LASTEXITCODE
} catch {
    Write-Warning $_.Exception.Message
    $resultCode = 1
} finally { Pop-Location }
Write-RotationLog 'finish' $resultCode
if ($resultCode -ne 0) {
    Write-Warning 'Stopped without replay. Check quota/auth/guard and partial work; record a handoff before using -SkipAgent.'
}
exit $resultCode
