// Роуты локального приложения Morphy: чтение проекта (GET) и управляющий контур
// экрана (POST arm → удержание по часам сервера → commit). Команды — только
// фиксированные модули Python через execFile, без shell.
import { execFile } from 'node:child_process';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { promisify } from 'node:util';
import path from 'node:path';

const execFileAsync = promisify(execFile);

// Зеркало src/morphy_actions.SPECS (сверяет tests/test_morphy_actions.py).
// safety — сторона безопасности: если состояние не прочитано, arm не блокируется
// (Python всё равно перепроверит предусловия в момент выполнения).
export const ACTIONS: Readonly<Record<string, Readonly<{ holdMs: number; phrase: string | null; safety: boolean }>>> = Object.freeze({
  'kill.engage': { holdMs: 2000, phrase: null, safety: true },
  'orders.cancel_all': { holdMs: 2000, phrase: null, safety: true },
  'engine.pause': { holdMs: 2000, phrase: null, safety: false },
  'engine.resume': { holdMs: 2000, phrase: null, safety: false },
  'breaker.reset.daily': { holdMs: 2500, phrase: 'СНЯТЬ DAILY', safety: false },
  'breaker.reset.global': { holdMs: 2500, phrase: 'СНЯТЬ GLOBAL', safety: false },
  'kill.reset': { holdMs: 2500, phrase: 'СНЯТЬ KILL', safety: false },
});
// Фраза сброса уходит в Python только этой переменной окружения. Имя содержит
// reset_breaker: guard проекта отклоняет команды агентов, где оно встречается.
export const CONFIRM_ENV = 'MORPHY_UI_RESET_BREAKER_CONFIRM';

const ARM_PATH = '/api/project/action/arm';
const COMMIT_PATH = '/api/project/action/commit';
const AI_TOOL_PATH = '/api/project/ai/tool';
const MAX_AI_BODY = 20_000;
const AI_TOOLS = new Set(['panel.state', 'board.task', 'runs.list', 'run.get', 'run.events',
  'run.result', 'ai.usage', 'ai.evals', 'assistant.draft', 'assistant.role',
  'assistant.handoff', 'task.handoff', 'assistant.review', 'task.review', 'assistant.specialist']);
const MAX_BODY = 2048;
const ARM_TTL_MS = 30_000;
const ARM_TTL_PHRASE_MS = 120_000;
const RUN_TIMEOUT_MS = 120_000;
const EXPIRED_GRACE_MS = 60_000;
const STATE_TTL_MS = 10_000;
const MAX_ARMS = 32;
const MAX_ARMS_PER_SESSION = 4;
const SUMMARY_MAX = 300;
const OUTCOMES = new Set(['ok', 'failed', 'rejected']);

type ActionResult = Record<string, unknown>;
type Exec = (file: string, args: string[], options: Record<string, unknown>) => Promise<{ stdout: string }>;
type Options = {
  validate?: (token: string) => Promise<boolean>;
  collect?: (task?: string) => Promise<any>;
  describe?: () => Promise<any>;
  run?: (action: string, confirm: string | null) => Promise<ActionResult | null>;
  now?: () => number;
  exec?: Exec;
  tool?: (name: string, arguments_: Record<string, unknown>) => Promise<any>;
};
type Arm = { action: string; session: Buffer; armedAt: number; holdMs: number; expiresAt: number; phrase: string | null };
type Body = { ok: true; value: unknown } | { ok: false; status: number; error: string };

const isObject = (value: unknown): value is Record<string, any> =>
  typeof value === 'object' && value !== null && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
const onlyKeys = (value: Record<string, unknown>, allowed: string[]) => Object.keys(value).every(key => allowed.includes(key));
const isAction = (name: unknown): name is string => typeof name === 'string' && Object.prototype.hasOwnProperty.call(ACTIONS, name);
const sessionOf = (token: string) => createHash('sha256').update(token, 'utf8').digest();
const sameSession = (a: Buffer, b: unknown) => Buffer.isBuffer(b) && a.length === b.length && timingSafeEqual(a, b);
const routeOf = (req: any) => String(req.originalUrl ?? `${req.baseUrl || ''}${req.path || ''}`).split('?')[0];

/** Последняя строка stdout `python -m src.morphy_actions run` — JSON {ok, action, summary, ...}. */
export function parseActionOutput(stdout: unknown): ActionResult | null {
  if (typeof stdout !== 'string') return null;
  const lines = stdout.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (!lines.length) return null;
  try {
    const value = JSON.parse(lines[lines.length - 1]);
    return isObject(value) && typeof value.ok === 'boolean' ? value : null;
  } catch { return null; }
}

// details — только счётчики, флаги и короткие слова (scope); вывод команд не проходит.
function cleanDetails(value: unknown): Record<string, unknown> | null {
  if (!isObject(value)) return null;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value).slice(0, 8)) {
    if (!/^[a-z_]{1,32}$/.test(key) || ['stdout', 'stderr', 'output'].includes(key)) continue;
    if (item === null || typeof item === 'boolean' || (typeof item === 'number' && Number.isFinite(item))) out[key] = item;
    else if (typeof item === 'string' && /^[a-z]{1,16}$/.test(item)) out[key] = item;
  }
  return Object.keys(out).length ? out : null;
}

// Ответ клиенту: allowlist полей, action — серверный, а не из вывода исполнителя.
function publicResult(action: string, raw: ActionResult | null) {
  const valid = isObject(raw) && raw.action === action;
  const ok = valid && raw.ok === true;
  let summary = valid && typeof raw.summary === 'string' && raw.summary.trim() ? raw.summary.slice(0, SUMMARY_MAX) : '';
  if (!summary) summary = raw === null ? 'Исполнитель не ответил — проверьте состояние вручную'
    : valid ? (ok ? 'Выполнено' : 'Действие не выполнено') : 'Ответ исполнителя не распознан — проверьте состояние вручную';
  const outcome = valid && OUTCOMES.has(raw.outcome as string) ? raw.outcome as string : ok ? 'ok' : 'failed';
  const details = valid ? cleanDetails(raw.details) : null;
  return { ok, action, summary, outcome, ...(details ? { details } : {}) };
}

function bodyProblem(req: any, maxBody = MAX_BODY): [number, string] | null {
  const type = String(req.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
  if (type !== 'application/json') return [415, 'json-required'];
  const length = req.headers['content-length'];
  if (length === undefined) return [411, 'length-required'];
  if (!/^\d{1,9}$/.test(String(length))) return [400, 'invalid-length'];
  if (Number(length) > maxBody) return [413, 'body-too-large'];
  return null;
}

async function readJson(req: any, maxBody = MAX_BODY): Promise<Body> {
  if (req.body !== undefined) {
    // Уже разобрано глобальным express.json() Morphy (index.ts): только сверка размера
    if (Buffer.byteLength(JSON.stringify(req.body) ?? '', 'utf8') > maxBody) return { ok: false, status: 413, error: 'body-too-large' };
    return { ok: true, value: req.body };
  }
  if (typeof req[Symbol.asyncIterator] !== 'function') return { ok: false, status: 400, error: 'invalid-json' };
  // Запасной путь без глобального парсера; длина уже проверена по Content-Length
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const part = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    size += part.length;
    if (size > maxBody) return { ok: false, status: 413, error: 'body-too-large' };
    chunks.push(part);
  }
  try { return { ok: true, value: JSON.parse(Buffer.concat(chunks).toString('utf8')) }; }
  catch { return { ok: false, status: 400, error: 'invalid-json' }; }
}

export function mountProjectRoutes(app: any, options: Options = {}) {
  // deployed file: <project>/data/morphy-eval/package/workspace/backend/project.ts
  const project = path.resolve(import.meta.dirname, '../../../../..');
  const python = path.join(project, '.venv', 'Scripts', 'python.exe');
  const supervisorPort = Number(process.env.SUPERVISOR_PORT || 7480);
  const allowedHosts = new Set([`127.0.0.1:${supervisorPort}`, `localhost:${supervisorPort}`,
                               `127.0.0.1:${supervisorPort + 4}`, `localhost:${supervisorPort + 4}`]);
  const allowedOrigins = new Set([`http://127.0.0.1:${supervisorPort}`, `http://localhost:${supervisorPort}`]);
  // Монотонные часы сервера: удержание и TTL не зависят от перевода системного времени
  const now = options.now || (() => performance.now());
  const exec: Exec = options.exec || ((file, args, opts) => execFileAsync(file, args, opts as any) as Promise<{ stdout: string }>);
  const pythonEnv = (extra: Record<string, string> = {}) => {
    const env: Record<string, string | undefined> = { ...process.env, PYTHONIOENCODING: 'utf-8' };
    delete env[CONFIRM_ENV];
    return { ...env, ...extra };
  };
  const runPython = (args: string[], timeout: number, maxBuffer: number, extra: Record<string, string> = {}) =>
    exec(python, args, { cwd: project, windowsHide: true, timeout, maxBuffer, env: pythonEnv(extra), shell: false });
  // Описание задачи передаётся по stdin: его нет в командной строке процесса.
  const tool = options.tool || ((name: string, arguments_: Record<string, unknown>) => new Promise<any>((resolve, reject) => {
    const child = execFile(python, ['-m', 'src.ai_tools_cli'], {
      cwd: project, windowsHide: true, timeout: 30_000, maxBuffer: 2 * 1024 * 1024,
      env: pythonEnv(), shell: false, encoding: 'utf8',
    }, (error, stdout) => {
      try { const reply = JSON.parse(stdout); resolve(reply); }
      catch { reject(new Error('ai-tool-unavailable')); }
    });
    child.stdin?.on('error', () => {});
    child.stdin?.end(JSON.stringify({ name, arguments: arguments_ }));
  }));

  const validate = options.validate || (async (token: string) => {
    try {
      const response = await fetch(`http://127.0.0.1:${supervisorPort}/api/portal/validate-token`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }), signal: AbortSignal.timeout(3000),
      });
      return response.ok && (await response.json()).valid === true;
    } catch { return false; }
  });
  const collect = options.collect || (async (task?: string) => {
    const { stdout } = await runPython(['-m', 'src.morphy_project', ...(task ? ['--task', task] : [])], 12000, 2 * 1024 * 1024);
    return JSON.parse(stdout);
  });
  const describeControls = options.describe || (async () => {
    const { stdout } = await runPython(['-m', 'src.morphy_actions', 'describe'], 15000, 256 * 1024);
    return JSON.parse(stdout);
  });
  const run = options.run || (async (action: string, confirm: string | null) => {
    try {
      const { stdout } = await runPython(['-m', 'src.morphy_actions', 'run', action], RUN_TIMEOUT_MS, 64 * 1024,
                                       confirm ? { [CONFIRM_ENV]: confirm } : {});
      return parseActionOutput(stdout);
    } catch (error: any) {
      // Код выхода 1 — штатный отказ: ответ всё равно в stdout. stderr не читается.
      const parsed = parseActionOutput(error?.stdout);
      if (parsed) return parsed;
      return { ok: false, action, outcome: 'failed', summary: error?.killed
        ? 'Исполнитель прерван по таймауту 120 с — проверьте состояние вручную'
        : 'Исполнитель действия завершился с ошибкой — проверьте состояние вручную' };
    }
  });

  // --- Чтение: кеш /state; commit сбрасывает его и отбрасывает начатый до него сбор ---
  let generation = 0;
  let cache: { at: number; gen: number; data: any } | null = null;
  let pending: { gen: number; promise: Promise<any> } | null = null;
  const loadState = (): Promise<any> => {
    if (cache && cache.gen === generation && now() - cache.at < STATE_TTL_MS) return Promise.resolve(cache.data);
    if (!pending || pending.gen !== generation) {
      const gen = generation;
      const promise: Promise<any> = Promise.resolve().then(() => collect()).then(data => {
        if (!data || data.schema_version !== 1) throw new Error('Unsupported schema');
        if (gen === generation) cache = { at: now(), gen, data };
        return data;
      }).finally(() => { if (pending && pending.promise === promise) pending = null; });
      pending = { gen, promise };
    }
    return pending.promise;
  };

  // --- Управление: одноразовые arm, один выполняющийся action на процесс ---
  const arms = new Map<string, Arm>();
  let busy = false;
  let describing: Promise<any> | null = null;
  // Истёкший arm ещё EXPIRED_GRACE_MS отвечает 410, потом удаляется и становится 404
  const prune = (t: number) => { for (const [id, arm] of arms) if (t >= arm.expiresAt + EXPIRED_GRACE_MS) arms.delete(id); };
  const enforceCaps = (session: Buffer) => {
    const own = [...arms.entries()].filter(([, arm]) => sameSession(arm.session, session));
    while (own.length > MAX_ARMS_PER_SESSION) arms.delete((own.shift() as [string, Arm])[0]);
    while (arms.size > MAX_ARMS) arms.delete(arms.keys().next().value as string);
  };
  const freshControls = () => {
    if (!describing) describing = Promise.resolve().then(() => describeControls()).finally(() => { describing = null; });
    return describing;
  };
  const availability = async (action: string): Promise<{ available: boolean; reason: string | null }> => {
    let controls: any;
    try { controls = await freshControls(); } catch { controls = undefined; }
    if (!isObject(controls) || !Array.isArray(controls.actions) || typeof controls.error === 'string') {
      return ACTIONS[action].safety ? { available: true, reason: null }
        : { available: false, reason: 'Не удалось прочитать состояние — повторите позже' };
    }
    if (controls.enabled !== true) return { available: false, reason: 'Управление доступно только в demo' };
    const item = controls.actions.find((entry: unknown) => isObject(entry) && entry.id === action);
    if (!isObject(item)) return { available: false, reason: 'Действие недоступно' };
    if (item.available === true) return { available: true, reason: null };
    return { available: false, reason: typeof item.reason === 'string' && item.reason ? item.reason.slice(0, 200) : 'Действие сейчас недоступно' };
  };
  const send = (res: any, code: number, body: Record<string, unknown>) => { res.status(code).json(body); };

  app.use('/api/project', async (req: any, res: any, next: any) => {
    res.set('Cache-Control', 'no-store');
    const site = req.headers['sec-fetch-site'];
    if (!allowedHosts.has(req.headers.host) || site === 'cross-site') {
      send(res, 403, { error: 'local-origin-required' }); return;
    }
    const origin = req.headers.origin;
    const post = req.method === 'POST';
    // POST строже GET: Origin обязателен и только свой, Sec-Fetch-Site (если есть) — same-origin
    const originBad = post ? !allowedOrigins.has(origin) || (site !== undefined && site !== 'same-origin')
                           : Boolean(origin) && !allowedOrigins.has(origin);
    if (originBad) { send(res, 403, { error: 'local-origin-required' }); return; }
    const route = routeOf(req);
    const action = post && (route === ARM_PATH || route === COMMIT_PATH);
    const aiTool = post && route === AI_TOOL_PATH;
    if (req.method !== 'GET' && !action && !aiTool) { send(res, 405, { error: 'read-only' }); return; }
    if (action || aiTool) {
      const problem = bodyProblem(req, aiTool ? MAX_AI_BODY : MAX_BODY);
      if (problem) { send(res, problem[0], { error: problem[1] }); return; }
    }
    const match = /^Bearer (\S{1,4096})$/.exec(req.headers.authorization || '');
    if (!match || !await validate(match[1])) {
      send(res, 401, { error: 'morphy-login-required' }); return;
    }
    req.morphySession = sessionOf(match[1]);
    next();
  });
  app.get('/api/project/state', async (_req: any, res: any) => {
    try { res.json(await loadState()); }
    catch { res.status(503).json({ error: 'project-data-unavailable' }); }
  });
  app.get('/api/project/task/:id', async (req: any, res: any) => {
    if (!/^[A-Z0-9_-]{1,80}$/.test(req.params.id)) { res.status(400).json({ error: 'invalid-task-id' }); return; }
    try { res.json(await collect(req.params.id)); }
    catch { res.status(404).json({ error: 'task-unavailable' }); }
  });

  app.post(AI_TOOL_PATH, async (req: any, res: any) => {
    const body = await readJson(req, MAX_AI_BODY);
    if (!body.ok) { send(res, body.status, { error: body.error }); return; }
    const value = body.value;
    if (!isObject(value) || !onlyKeys(value, ['name', 'arguments']) || typeof value.name !== 'string'
        || !AI_TOOLS.has(value.name) || !isObject(value.arguments)) {
      send(res, 400, { error: 'invalid-ai-tool' }); return;
    }
    try {
      const reply = await tool(value.name, value.arguments);
      if (!isObject(reply) || typeof reply.ok !== 'boolean') {
        send(res, 503, { error: 'ai-tool-unavailable' }); return;
      }
      // Подробный текст исключения не выдаём: он может содержать аргумент запроса.
      if (!reply.ok) { send(res, 400, { error: 'invalid-ai-arguments' }); return; }
      res.json(reply);
    } catch { send(res, 503, { error: 'ai-tool-unavailable' }); }
  });

  app.post(ARM_PATH, async (req: any, res: any) => {
    const armedAt = now();          // удержание отсчитывается от прихода arm
    const session = req.morphySession;
    if (!Buffer.isBuffer(session)) { send(res, 401, { error: 'morphy-login-required' }); return; }
    const body = await readJson(req);
    if (!body.ok) { send(res, body.status, { error: body.error }); return; }
    const value = body.value;
    if (!isObject(value) || !onlyKeys(value, ['action']) || !isAction(value.action)) {
      send(res, 400, { error: 'invalid-action' }); return;
    }
    const action = value.action;
    if (busy) { send(res, 423, { error: 'busy' }); return; }
    const verdict = await availability(action);
    if (!verdict.available) { send(res, 409, { error: 'unavailable', reason: verdict.reason }); return; }
    if (busy) { send(res, 423, { error: 'busy' }); return; }
    const spec = ACTIONS[action];
    const ttl = spec.phrase ? ARM_TTL_PHRASE_MS : ARM_TTL_MS;
    const t = now();
    prune(t);
    const armId = randomBytes(16).toString('hex');   // 128 бит
    arms.set(armId, { action, session, armedAt, holdMs: spec.holdMs, expiresAt: armedAt + ttl, phrase: spec.phrase });
    enforceCaps(session);
    res.json({ arm_id: armId, action, min_hold_ms: spec.holdMs,
               expires_in_s: Math.max(0, Math.floor((armedAt + ttl - t) / 1000)), confirm_phrase: spec.phrase });
  });

  app.post(COMMIT_PATH, async (req: any, res: any) => {
    const body = await readJson(req);
    if (!body.ok) { send(res, body.status, { error: body.error }); return; }
    const value = body.value;
    const confirmOk = (c: unknown) => c === undefined || c === null || (typeof c === 'string' && c.length <= 64);
    if (!isObject(value) || !onlyKeys(value, ['arm_id', 'confirm']) || typeof value.arm_id !== 'string' || !confirmOk(value.confirm)) {
      send(res, 400, { error: 'invalid-request' }); return;
    }
    // Дальше до run() — без await: проверка и погашение arm атомарны для event loop
    const t = now();
    prune(t);
    const armId: string = value.arm_id;
    const arm = /^[0-9a-f]{32}$/.test(armId) ? arms.get(armId) : undefined;
    // Чужая сессия неотличима от несуществующего arm и не гасит его
    if (!arm || !sameSession(arm.session, req.morphySession)) { send(res, 404, { error: 'unknown-arm' }); return; }
    if (t >= arm.expiresAt) { arms.delete(armId); send(res, 410, { error: 'arm-expired' }); return; }
    if (t < arm.armedAt + arm.holdMs) {
      send(res, 409, { error: 'hold-too-short', remaining_ms: Math.ceil(arm.armedAt + arm.holdMs - t) }); return;
    }
    if (arm.phrase !== null && value.confirm !== arm.phrase) {
      arms.delete(armId); send(res, 403, { error: 'confirm-mismatch' }); return;
    }
    if (busy) { send(res, 423, { error: 'busy' }); return; }
    arms.delete(armId);
    busy = true;
    let raw: ActionResult | null = null;
    try { raw = await run(arm.action, arm.phrase); }
    catch { raw = null; }
    finally { busy = false; generation += 1; cache = null; }
    const result = publicResult(arm.action, raw);
    if (result.ok) res.json(result);
    else send(res, 502, { error: 'action-failed', ...result });
  });

  // Ошибки глобального express.json() и маршрутов /api/project — без текста и стека
  app.use('/api/project', (err: any, _req: any, res: any, next: any) => {
    if (res.headersSent) { next(err); return; }
    res.set('Cache-Control', 'no-store');
    const status = Number(err?.status || err?.statusCode);
    if (status === 413) { send(res, 413, { error: 'body-too-large' }); return; }
    if (status === 415) { send(res, 415, { error: 'json-required' }); return; }
    if (status >= 400 && status < 500) { send(res, 400, { error: 'invalid-json' }); return; }
    send(res, 500, { error: 'internal-error' });
  });
}
