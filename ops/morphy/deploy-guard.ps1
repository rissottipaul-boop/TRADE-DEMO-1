# Подключение общего guard проекта к AI-агенту Morphy (MORPHY-GUARD-WIRE,
# MORPHY-GUARD-CODEX). Копирует ops/morphy/guard-hook.mjs в установленный пакет
# и точечно подключает:
#  - harnesses/claude.ts: hooks: guardHooks() во все три query()-сайта
#    (живой разговор, one-shot pulse/cron/customer, agent-API);
#  - harnesses/pi/session.ts: pre-check piGuardDeny в executeTool;
#  - harnesses/codex.ts: approvalPolicy 'untrusted' + решение approval-запросов
#    app-server через codexGuardDecision (одноразовый accept, не for-session).
# Правок периметра (ops/hooks/) и аккаунтов нет. Идемпотентен.
$ErrorActionPreference = 'Stop'
$guardProject = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$guardPackage = Join-Path $guardProject 'data/morphy-eval/package'
$guardSupervisor = Join-Path $guardPackage 'supervisor'
$guardVersion = (Get-Content -LiteralPath (Join-Path $guardPackage 'package.json') -Raw | ConvertFrom-Json).version
if ($guardVersion -ne '0.5.0') { throw 'Патч рассчитан на Morphy 0.5.0; другую версию сначала сверить.' }

Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'guard-hook.mjs') -Destination (Join-Path $guardSupervisor 'guard-hook.mjs')
$guardUtf8 = New-Object System.Text.UTF8Encoding($false)

# ── claude.ts: PreToolUse hooks Claude Agent SDK ─────────────────────────────
$guardClaudePath = Join-Path $guardSupervisor 'harnesses/claude.ts'
$guardClaude = [IO.File]::ReadAllText($guardClaudePath)
if (-not $guardClaude.Contains("import { guardHooks } from '../guard-hook.mjs';")) {
    $guardAnchor = "import { mirrorSkillsInto } from './skills.js';"
    if (-not $guardClaude.Contains($guardAnchor)) { throw 'claude.ts изменился: точка импорта не найдена, нужен ручной разбор.' }
    $guardClaude = $guardClaude.Replace($guardAnchor, $guardAnchor + "`nimport { guardHooks } from '../guard-hook.mjs';")
    $guardSite = 'allowDangerouslySkipPermissions: true,'
    $guardSites = ([regex]::Matches($guardClaude, [regex]::Escape($guardSite))).Count
    if ($guardSites -ne 3) { throw "claude.ts изменился: ожидалось 3 query()-сайта, найдено $guardSites." }
    $guardClaude = $guardClaude.Replace($guardSite, $guardSite + "`n        hooks: guardHooks(), // guard проекта (MORPHY-GUARD-WIRE)")
    [IO.File]::WriteAllText($guardClaudePath, $guardClaude, $guardUtf8)
}

# ── pi/session.ts: pre-check в executeTool ───────────────────────────────────
$guardPiPath = Join-Path $guardSupervisor 'harnesses/pi/session.ts'
$guardPi = [IO.File]::ReadAllText($guardPiPath)
if (-not $guardPi.Contains("import { piGuardDeny } from '../../guard-hook.mjs';")) {
    $guardPiAnchor = "import { findTool } from './tools/registry.js';"
    if (-not $guardPi.Contains($guardPiAnchor)) { throw 'pi/session.ts изменился: точка импорта не найдена.' }
    $guardPi = $guardPi.Replace($guardPiAnchor, $guardPiAnchor + "`nimport { piGuardDeny } from '../../guard-hook.mjs';")
    $guardPiSite = '      return await tool.run(call.input, { cwd: init.cwd, signal: init.abortController.signal, tasks: init.taskHost });'
    if (-not $guardPi.Contains($guardPiSite)) { throw 'pi/session.ts изменился: executeTool не найден, нужен ручной разбор.' }
    # piGuardDeny не бросает исключений (fail-open), размещение внутри try безопасно.
    $guardPiCheck = '      const guardReason = await piGuardDeny(call.name, call.input); // guard проекта (MORPHY-GUARD-WIRE)' + "`n" +
        '      if (guardReason) return { output: guardReason, isError: true };' + "`n"
    $guardPi = $guardPi.Replace($guardPiSite, $guardPiCheck + $guardPiSite)
    [IO.File]::WriteAllText($guardPiPath, $guardPi, $guardUtf8)
}

# ── codex.ts: approval-запросы app-server через guard ────────────────────────
$guardCodexPath = Join-Path $guardSupervisor 'harnesses/codex.ts'
$guardCodex = [IO.File]::ReadAllText($guardCodexPath)
if (-not $guardCodex.Contains("import { codexGuardDecision } from '../guard-hook.mjs';")) {
    $guardCodexImport = "import type { OnAgentMessage, RecentMessage, AgentAttachment, AgentQueryRequest, AgentQueryResult } from './types.js';"
    if (-not $guardCodex.Contains($guardCodexImport)) { throw 'codex.ts изменился: точка импорта не найдена.' }
    $guardCodex = $guardCodex.Replace($guardCodexImport, $guardCodexImport + "`nimport { codexGuardDecision } from '../guard-hook.mjs';")

    # approvalPolicy 'never' отключает approval-запросы — без 'untrusted' guard не видит действий.
    $guardNever = "approvalPolicy: 'never',"
    $guardNeverCount = ([regex]::Matches($guardCodex, [regex]::Escape($guardNever))).Count
    if ($guardNeverCount -ne 2) { throw "codex.ts изменился: ожидалось 2 сайта approvalPolicy, найдено $guardNeverCount." }
    $guardCodex = $guardCodex.Replace($guardNever, "approvalPolicy: 'untrusted', // guard проекта (MORPHY-GUARD-CODEX): approval-запросы идут в guard-мост")

    # Авто-accept заменяется одноразовым решением guard (сервер ждёт ответа клиента).
    $guardSwitchOld = @(
        "      case 'item/commandExecution/requestApproval':",
        "      case 'item/fileChange/requestApproval':",
        '        log.info(`[codex-rpc] auto-accepting ${msg.method}`);',
        "        this.respond(msg.id, { decision: 'acceptForSession' });",
        '        return;',
        "      case 'execCommandApproval':",
        "      case 'applyPatchApproval':",
        '        log.info(`[codex-rpc] auto-accepting (legacy) ${msg.method}`);',
        "        this.respond(msg.id, { decision: 'approved_for_session' });",
        '        return;'
    ) -join "`n"
    if (-not $guardCodex.Contains($guardSwitchOld)) { throw 'codex.ts изменился: блок авто-accept не найден, нужен ручной разбор.' }
    $guardSwitchNew = @(
        "      case 'item/commandExecution/requestApproval':",
        "      case 'item/fileChange/requestApproval':",
        "      case 'execCommandApproval':",
        "      case 'applyPatchApproval':",
        '        // guard проекта (MORPHY-GUARD-CODEX): одноразовое решение на каждый запрос',
        '        void codexGuardDecision(msg.method, msg.params).then((d) => {',
        '          log.info(`[codex-rpc] guard ${d.decision} ${msg.method}${d.reason ? ` — ${d.reason}` : ``}`);',
        '          this.respond(msg.id, { decision: d.decision });',
        '        });',
        '        return;'
    ) -join "`n"
    $guardCodex = $guardCodex.Replace($guardSwitchOld, $guardSwitchNew)
    [IO.File]::WriteAllText($guardCodexPath, $guardCodex, $guardUtf8)
}

'Guard подключён: claude.ts (3 query()-сайта), pi/session.ts (executeTool), codex.ts (approval-запросы).'
'Перезапустить Morphy: ops\morphy\trial.ps1 stop; ops\morphy\trial.ps1 start'
