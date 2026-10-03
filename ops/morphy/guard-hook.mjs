// Мост Morphy → общий guard проекта (задача MORPHY-GUARD-WIRE).
//
// Файл деплоится скриптом ops/morphy/deploy-guard.ps1 в
// data/morphy-eval/package/supervisor/guard-hook.mjs и подключается:
//  - в harnesses/claude.ts — как PreToolUse hook Claude Agent SDK
//    (Options.hooks, callback возвращает hookSpecificOutput.permissionDecision);
//  - в harnesses/pi/tools-периметре (session.ts executeTool) — как pre-check
//    перед запуском инструмента pi-harness;
//  - в harnesses/codex.ts — как решение approval-запросов Codex app-server
//    (MORPHY-GUARD-CODEX, approvalPolicy 'untrusted').
//
// Сам правил НЕ содержит: каждое решение принимает ops/hooks/guard.py через
// ops/hooks/guard_adapter.py (--client claude: pi/SDK инструменты Read/Write/
// Edit/Bash зеркалят формат Claude Code). Контракт как у guard: allow — пустой
// вывод адаптера; сбой моста/адаптера — fail-open с записью в лог supervisor.
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const TIMEOUT_MS = 30_000;

// Корень проекта ищем вверх от файла (как _find_root адаптера): работает и для
// git-копии в ops/morphy, и для задеплоенной в data/morphy-eval/package/supervisor.
function findRoot() {
  let dir = path.dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 12; i += 1) {
    if (fs.existsSync(path.join(dir, 'ops', 'hooks', 'guard_adapter.py'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

const ROOT = findRoot();
const ADAPTER = ROOT ? path.join(ROOT, 'ops', 'hooks', 'guard_adapter.py') : null;

function findPython() {
  if (!ROOT) return 'python';
  const candidates = [
    path.join(ROOT, '.venv', 'Scripts', 'python.exe'), // Windows
    path.join(ROOT, '.venv', 'bin', 'python'),         // POSIX
  ];
  return candidates.find((p) => fs.existsSync(p)) || 'python';
}

const PYTHON = findPython();

/**
 * Прогоняет полезную нагрузку hook через guard_adapter (--client claude).
 * Возвращает разобранный JSON решения адаптера или null (allow / fail-open).
 */
export function runGuardAdapter(payload) {
  return new Promise((resolve) => {
    if (!ADAPTER) {
      console.error('[guard-hook] ops/hooks/guard_adapter.py не найден — fail-open (контракт guard)');
      resolve(null);
      return;
    }
    const child = execFile(
      PYTHON,
      [ADAPTER, '--client', 'claude'],
      { cwd: ROOT, timeout: TIMEOUT_MS, windowsHide: true, maxBuffer: 1024 * 1024 },
      (err, stdout) => {
        if (err) {
          console.error(`[guard-hook] адаптер не выполнился — fail-open: ${err.message}`);
          resolve(null);
          return;
        }
        const text = (stdout || '').trim();
        if (!text) { resolve(null); return; } // allow — пустой вывод по контракту guard
        try {
          resolve(JSON.parse(text));
        } catch {
          console.error('[guard-hook] не-JSON ответ адаптера — fail-open');
          resolve(null);
        }
      },
    );
    // Windows: EPIPE при мгновенном завершении python не должен ронять supervisor.
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

/**
 * HookCallback Claude Agent SDK для события PreToolUse.
 * Формат входа SDK совпадает с входом guard: {tool_name, tool_input}.
 * Deny адаптера уже в формате SDK: hookSpecificOutput.permissionDecision = "deny".
 */
export async function guardPreToolUse(input) {
  const decision = await runGuardAdapter({
    hook_event_name: 'PreToolUse',
    tool_name: input?.tool_name ?? '',
    tool_input: input?.tool_input ?? {},
  });
  if (decision && typeof decision === 'object' && decision.hookSpecificOutput) {
    return decision;
  }
  return {};
}

/** Значение для Options.hooks query() Claude Agent SDK. */
export function guardHooks() {
  return { PreToolUse: [{ hooks: [guardPreToolUse], timeout: Math.ceil(TIMEOUT_MS / 1000) }] };
}

/**
 * Pre-check для pi-harness (executeTool): причина отказа строкой или null (allow).
 * Имена и входы инструментов pi зеркалят Claude SDK, поэтому формат тот же.
 */
export async function piGuardDeny(toolName, toolInput) {
  const decision = await runGuardAdapter({
    hook_event_name: 'PreToolUse',
    tool_name: toolName ?? '',
    tool_input: toolInput ?? {},
  });
  const out = decision && typeof decision === 'object' ? decision.hookSpecificOutput : null;
  if (out && out.permissionDecision === 'deny') {
    return out.permissionDecisionReason || '[guard] запрещено guard проекта';
  }
  return null;
}

// ── Codex app-server (MORPHY-GUARD-CODEX) ───────────────────────────────────
// Harness Codex исполняет инструменты внутри процесса `codex app-server`;
// единственная штатная точка — approval-запросы протокола (сервер ждёт ответа
// клиента): v2 `item/commandExecution|fileChange/requestApproval` с ответом
// CommandExecution/FileChangeApprovalDecision и legacy `execCommandApproval` /
// `applyPatchApproval` с ReviewDecision. Они приходят только при
// approvalPolicy != 'never' — deploy-guard.ps1 ставит 'untrusted'.

function extractCommandText(params) {
  const c = params?.command ?? params?.cmd ?? params?.commandLine ?? null;
  if (Array.isArray(c)) return c.map(String).join(' ');
  if (typeof c === 'string') return c;
  return '';
}

function extractChangedPaths(params) {
  const out = [];
  for (const container of [params?.fileChanges, params?.file_changes, params?.changes, params?.files]) {
    if (!container) continue;
    if (Array.isArray(container)) {
      for (const item of container) {
        if (typeof item === 'string') out.push(item);
        else if (item && typeof item === 'object') {
          for (const key of ['path', 'file_path', 'filePath']) {
            if (typeof item[key] === 'string') out.push(item[key]);
          }
        }
      }
    } else if (typeof container === 'object') {
      out.push(...Object.keys(container));
    }
  }
  if (typeof params?.path === 'string') out.push(params.path);
  return out;
}

/**
 * Решение guard для approval-запроса Codex app-server.
 * Возвращает { decision, reason }: decision в словаре соответствующего метода.
 * Allow — одноразовый accept/approved (НЕ *ForSession: каждый следующий вызов
 * снова проходит guard). Нераспознанная форма запроса — отказ в сторону
 * безопасности, с причиной в reason (видна в логе supervisor).
 */
export async function codexGuardDecision(method, params) {
  const legacy = method === 'execCommandApproval' || method === 'applyPatchApproval';
  const isCommand = method === 'execCommandApproval' || method === 'item/commandExecution/requestApproval';
  const deny = (reason) => ({ decision: legacy ? 'denied' : 'decline', reason });
  let reason = null;
  if (isCommand) {
    const command = extractCommandText(params);
    if (!command) return deny('[guard-hook] approval без текста команды — отказ в сторону безопасности');
    reason = await piGuardDeny('Bash', { command });
  } else {
    const paths = extractChangedPaths(params);
    if (!paths.length) return deny('[guard-hook] approval без путей изменяемых файлов — отказ в сторону безопасности');
    for (const p of paths) {
      reason = await piGuardDeny('Write', { file_path: p, content: 'x' });
      if (reason) break;
    }
  }
  if (reason) return deny(reason);
  return { decision: legacy ? 'approved' : 'accept', reason: null };
}
