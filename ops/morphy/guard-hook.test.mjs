// Проверка моста guard-hook.mjs канарейками из insights/guard-compat.md
// (задача MORPHY-GUARD-WIRE). Тестируется ЗАДЕПЛОЕННАЯ копия моста из
// установленного пакета Morphy — та, которую реально импортирует supervisor, —
// и наличие точек подключения в патченных harness-файлах.
// Запуск из корня проекта:  node ops/morphy/guard-hook.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const deployed = path.join(root, 'data/morphy-eval/package/supervisor/guard-hook.mjs');
assert.ok(fs.existsSync(deployed), 'мост не задеплоен: сначала ops/morphy/deploy-guard.ps1');

// Разведка формы вызовов в журнале guard — через env, файл data/GUARD_TRACE не создаём.
process.env.AGENT_GUARD_TRACE = '1';

const { guardPreToolUse, piGuardDeny, codexGuardDecision } = await import(pathToFileURL(deployed).href);

const guardLog = path.join(root, 'data', 'guard.log');
const logLinesBefore = fs.existsSync(guardLog)
  ? fs.readFileSync(guardLog, 'utf-8').split('\n').filter(Boolean).length
  : 0;

let passed = 0;
async function check(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

// Канарейка-контроль: безвредная команда должна пройти (иначе hook блокирует всё подряд).
await check('контроль: echo guard-e2e-ok проходит', async () => {
  const out = await guardPreToolUse({ tool_name: 'Bash', tool_input: { command: 'echo guard-e2e-ok' } });
  assert.deepEqual(out, {}, `ожидался allow, получено: ${JSON.stringify(out)}`);
});

// Канарейка 2: guard запрещает слово withdraw; при пропуске команда лишь печатает строку.
await check('канарейка 2: echo withdraw-canary отклоняется с [guard]', async () => {
  const out = await guardPreToolUse({ tool_name: 'Bash', tool_input: { command: 'echo withdraw-canary' } });
  const spec = out?.hookSpecificOutput;
  assert.equal(spec?.permissionDecision, 'deny', `ожидался deny, получено: ${JSON.stringify(out)}`);
  assert.ok(String(spec?.permissionDecisionReason || '').startsWith('[guard]'),
    `причина без [guard]: ${spec?.permissionDecisionReason}`);
});

// Канарейка 3: запись в периметр .github/hooks/ должна отклоняться.
await check('канарейка 3: Write .github/hooks/e2e-canary.txt отклоняется', async () => {
  const out = await guardPreToolUse({
    tool_name: 'Write',
    tool_input: { file_path: '.github/hooks/e2e-canary.txt', content: 'x' },
  });
  assert.equal(out?.hookSpecificOutput?.permissionDecision, 'deny',
    `ожидался deny, получено: ${JSON.stringify(out)}`);
});

// Тот же мост для pi-harness: deny строкой, allow — null.
await check('pi-harness: piGuardDeny отклоняет канарейку и пропускает контроль', async () => {
  const deny = await piGuardDeny('Bash', { command: 'echo withdraw-canary' });
  assert.ok(deny && deny.startsWith('[guard]'), `ожидался [guard]-отказ, получено: ${deny}`);
  const allow = await piGuardDeny('Bash', { command: 'echo guard-e2e-ok' });
  assert.equal(allow, null, `ожидался allow (null), получено: ${allow}`);
});

// Журнал guard: появились записи deny с client=claude (решения пишет сам guard/адаптер).
await check('data/guard.log: добавлены deny с client=claude', async () => {
  const lines = fs.readFileSync(guardLog, 'utf-8').split('\n').filter(Boolean);
  assert.ok(lines.length > logLinesBefore, 'журнал guard не вырос');
  const fresh = lines.slice(logLinesBefore).map((l) => { try { return JSON.parse(l); } catch { return {}; } });
  const denies = fresh.filter((e) => e.decision === 'deny' && e.client === 'claude');
  assert.ok(denies.length >= 3, `ожидалось >=3 deny client=claude, найдено ${denies.length}`);
});

// Codex app-server (MORPHY-GUARD-CODEX): approval-запросы обоих словарей протокола.
await check('codex: legacy execCommandApproval — канарейка denied, контроль approved (одноразово)', async () => {
  const deny = await codexGuardDecision('execCommandApproval', { command: ['echo', 'withdraw-canary'], cwd: root });
  assert.equal(deny.decision, 'denied', `ожидался denied, получено: ${JSON.stringify(deny)}`);
  assert.ok(String(deny.reason || '').startsWith('[guard]'), `причина без [guard]: ${deny.reason}`);
  const allow = await codexGuardDecision('execCommandApproval', { command: ['echo', 'guard-e2e-ok'] });
  assert.equal(allow.decision, 'approved', `ожидался одноразовый approved, получено: ${JSON.stringify(allow)}`);
});

await check('codex: v2 requestApproval — канарейка decline, контроль accept (одноразово)', async () => {
  const deny = await codexGuardDecision('item/commandExecution/requestApproval', { command: 'echo withdraw-canary' });
  assert.equal(deny.decision, 'decline', `ожидался decline, получено: ${JSON.stringify(deny)}`);
  const allow = await codexGuardDecision('item/commandExecution/requestApproval', { command: 'echo guard-e2e-ok' });
  assert.equal(allow.decision, 'accept', `ожидался одноразовый accept, получено: ${JSON.stringify(allow)}`);
});

await check('codex: applyPatchApproval в периметр .github/hooks/ отклоняется', async () => {
  const deny = await codexGuardDecision('applyPatchApproval', {
    fileChanges: { '.github/hooks/e2e-canary.txt': { type: 'add', content: 'x' } },
  });
  assert.equal(deny.decision, 'denied', `ожидался denied, получено: ${JSON.stringify(deny)}`);
  const allow = await codexGuardDecision('item/fileChange/requestApproval', {
    changes: [{ path: 'data/morphy-eval/scratch.txt' }],
  });
  assert.equal(allow.decision, 'accept', `ожидался accept, получено: ${JSON.stringify(allow)}`);
});

await check('codex: нераспознанная форма approval — отказ в сторону безопасности', async () => {
  const noCmd = await codexGuardDecision('execCommandApproval', {});
  assert.equal(noCmd.decision, 'denied', `ожидался denied, получено: ${JSON.stringify(noCmd)}`);
  const noPaths = await codexGuardDecision('applyPatchApproval', {});
  assert.equal(noPaths.decision, 'denied', `ожидался denied, получено: ${JSON.stringify(noPaths)}`);
});

// Точки подключения в установленных harness-файлах (после deploy-guard.ps1).
await check('патч на месте: claude.ts (3 сайта), pi/session.ts и codex.ts', async () => {
  const claude = fs.readFileSync(path.join(root, 'data/morphy-eval/package/supervisor/harnesses/claude.ts'), 'utf-8');
  const sites = (claude.match(/hooks: guardHooks\(\)/g) || []).length;
  assert.equal(sites, 3, `в claude.ts ожидалось 3 подключения hooks, найдено ${sites}`);
  assert.ok(claude.includes("import { guardHooks } from '../guard-hook.mjs';"), 'нет импорта в claude.ts');
  const pi = fs.readFileSync(path.join(root, 'data/morphy-eval/package/supervisor/harnesses/pi/session.ts'), 'utf-8');
  assert.ok(pi.includes('await piGuardDeny(call.name, call.input)'), 'нет pre-check в pi/session.ts');
  const codex = fs.readFileSync(path.join(root, 'data/morphy-eval/package/supervisor/harnesses/codex.ts'), 'utf-8');
  const untrusted = (codex.match(/approvalPolicy: 'untrusted',/g) || []).length;
  assert.equal(untrusted, 2, `в codex.ts ожидалось 2 сайта approvalPolicy 'untrusted', найдено ${untrusted}`);
  assert.ok(!codex.includes("approvalPolicy: 'never',"), "в codex.ts остался approvalPolicy 'never'");
  assert.ok(codex.includes('void codexGuardDecision(msg.method, msg.params)'), 'нет решения guard в codex.ts');
  assert.ok(!codex.includes('auto-accepting'), 'в codex.ts остался авто-accept approval-запросов');
});

console.log(`\n${passed} проверок пройдено.`);
