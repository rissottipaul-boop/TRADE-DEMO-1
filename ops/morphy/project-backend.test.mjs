// Проверка границы API без аккаунта/сессии человека, без торгового движка и без Python:
// исполнитель действий, describe, коллектор и часы сервера подменены фейками.
import test from 'node:test';
import assert from 'node:assert/strict';
import { ACTIONS, CONFIRM_ENV, mountProjectRoutes, parseActionOutput } from './project-backend.ts';

const ORIGIN = 'http://127.0.0.1:7480';
const SECRET = 'private-key=do-not-expose';
const TOKENS = new Set(['test-only-token', 'second-session-token']);

const controls = (overrides = {}) => ({
  mode: 'demo', enabled: true,
  actions: Object.keys(ACTIONS).map(id => ({ id, available: true, reason: null, ...(overrides[id] || {}) })),
});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(resolve => setImmediate(resolve));

// run/describe: undefined — фейк по умолчанию; null — настоящий код модуля поверх exec
function harness({ valid = true, collect = async () => ({ schema_version: 1, board: {} }), describe, run, exec } = {}) {
  const clock = { t: 1_000_000 };
  let middleware;
  let errorHandler;
  const routes = {};
  const seen = { collected: 0, validated: 0, described: 0, runs: [] };
  const options = {
    validate: async token => { seen.validated++; return valid && TOKENS.has(token); },
    collect: async task => { seen.collected++; return collect(task); },
    now: () => clock.t,
  };
  if (describe !== null) options.describe = async () => { seen.described++; return (describe || (() => controls()))(); };
  if (run !== null) {
    options.run = async (action, confirm) => {
      seen.runs.push([action, confirm]);
      return (run || (async a => ({ ok: true, action: a, summary: 'Готово', outcome: 'ok' })))(action, confirm);
    };
  }
  if (exec) options.exec = exec;
  mountProjectRoutes({
    use: (_path, fn) => { if (fn.length === 4) errorHandler = fn; else middleware = fn; },
    get: (path, fn) => { routes[`GET ${path}`] = fn; },
    post: (path, fn) => { routes[`POST ${path}`] = fn; },
  }, options);

  const request = async ({ path = '/api/project/state', method = 'GET', host = '127.0.0.1:7480', token = 'test-only-token',
                           origin, site, id = 'T1', body, raw, type, length, url } = {}) => {
    const req = {
      method, originalUrl: url ?? path.replace(':id', id), params: { id }, body,
      headers: { host, authorization: token ? `Bearer ${token}` : undefined, origin, 'sec-fetch-site': site,
                 'content-type': type, 'content-length': length },
    };
    if (raw !== undefined) req[Symbol.asyncIterator] = async function* () { yield Buffer.from(raw); };
    const res = {
      code: 200, data: null, headersSent: false, set() { return this; },
      status(n) { this.code = n; return this; }, json(data) { this.data = data; this.headersSent = true; return this; },
    };
    let passed = false;
    await middleware(req, res, () => { passed = true; });
    if (passed) await routes[`${method} ${path}`](req, res);
    return res;
  };
  // POST как у фронтенда: свой Origin, JSON, честный Content-Length
  const post = (path, body, extra = {}) => request({
    path, method: 'POST', origin: ORIGIN, site: 'same-origin', body, type: 'application/json',
    length: String(Buffer.byteLength(JSON.stringify(body ?? null))), ...extra,
  });
  const arm = (action, extra) => post('/api/project/action/arm', { action }, extra);
  const commit = (armId, confirm, extra) => post('/api/project/action/commit',
    confirm === undefined ? { arm_id: armId } : { arm_id: armId, confirm }, extra);
  return { request, post, arm, commit, clock, seen, errorHandler: () => errorHandler, count: () => seen.collected };
}

// --- Существующие границы чтения ---

test('без токена данные не читаются', async () => {
  const h = harness();
  assert.equal((await h.request({ token: null })).code, 401);
  assert.equal(h.count(), 0);
});
test('невалидная сессия отклоняется', async () => {
  const h = harness({ valid: false });
  assert.equal((await h.request()).code, 401);
  assert.equal(h.count(), 0);
});
test('чужой origin и DNS rebinding host отклоняются', async () => {
  for (const input of [{ origin: 'https://untrusted.test' }, { host: 'untrusted.test:7480' }, { site: 'cross-site' }]) {
    const h = harness();
    assert.equal((await h.request(input)).code, 403);
    assert.equal(h.count(), 0);
  }
});
test('операции записи запрещены: кроме arm/commit только GET', async () => {
  const h = harness();
  assert.equal((await h.request({ method: 'POST' })).code, 403, 'POST без Origin');
  for (const [method, url] of [['POST', '/api/project/state'], ['POST', '/api/project/task/T1'],
                               ['POST', '/api/project/action/other'], ['PUT', '/api/project/action/arm'],
                               ['DELETE', '/api/project/action/commit'], ['PATCH', '/api/project/state'],
                               ['OPTIONS', '/api/project/action/arm'], ['HEAD', '/api/project/state']]) {
    const response = await h.request({ method, url, origin: ORIGIN });
    assert.equal(response.code, 405, `${method} ${url}`);
  }
  assert.equal(h.count(), 0);
  assert.equal(h.seen.runs.length, 0);
  assert.equal(h.seen.validated, 0);
});
test('авторизованное чтение и кеш не повторяют коллектор', async () => {
  const h = harness();
  assert.equal((await h.request()).code, 200);
  assert.equal((await h.request()).code, 200);
  assert.equal(h.count(), 1);
});
test('некорректный ID не попадает в subprocess', async () => {
  const h = harness();
  assert.equal((await h.request({ path: '/api/project/task/:id', id: '../T1' })).code, 400);
  assert.equal(h.count(), 0);
});
test('ошибка коллектора не раскрывает сообщение', async () => {
  const h = harness({ collect: async () => { throw new Error(SECRET); } });
  const response = await h.request();
  assert.equal(response.code, 503);
  assert.equal(response.data.error, 'project-data-unavailable');
  assert.ok(!JSON.stringify(response).includes('do-not-expose'));
});
test('GET без изменений: Origin необязателен, свой Origin и same-site допустимы, кеш 10 с', async () => {
  const h = harness();
  assert.equal((await h.request()).code, 200);
  assert.equal((await h.request({ origin: ORIGIN })).code, 200);
  assert.equal((await h.request({ site: 'same-site' })).code, 200);
  assert.equal((await h.request({ path: '/api/project/task/:id', id: 'MORPHY-UI-1' })).code, 200);
  assert.equal(h.count(), 2);                       // state один раз + task
  h.clock.t += 10_000;
  assert.equal((await h.request()).code, 200);
  assert.equal(h.count(), 3);
});

// --- Управляющий контур ---

test('arm → удержание → commit: одноразовый arm_id на 128 бит', async () => {
  const h = harness();
  const armed = await h.arm('kill.engage');
  assert.equal(armed.code, 200);
  assert.match(armed.data.arm_id, /^[0-9a-f]{32}$/);
  assert.deepEqual({ ...armed.data, arm_id: 'x' },
                   { arm_id: 'x', action: 'kill.engage', min_hold_ms: 2000, expires_in_s: 30, confirm_phrase: null });
  const other = await h.arm('kill.engage');
  assert.notEqual(other.data.arm_id, armed.data.arm_id);
  h.clock.t += 2000;
  const done = await h.commit(armed.data.arm_id);
  assert.equal(done.code, 200);
  assert.deepEqual(done.data, { ok: true, action: 'kill.engage', summary: 'Готово', outcome: 'ok' });
  assert.deepEqual(h.seen.runs, [['kill.engage', null]]);
});
test('hold-too-short проверяется по часам сервера, arm остаётся валидным', async () => {
  const h = harness();
  const { arm_id } = (await h.arm('orders.cancel_all')).data;
  h.clock.t += 1999;
  const early = await h.commit(arm_id);
  assert.equal(early.code, 409);
  assert.deepEqual(early.data, { error: 'hold-too-short', remaining_ms: 1 });
  assert.equal(h.seen.runs.length, 0);
  h.clock.t += 1;
  assert.equal((await h.commit(arm_id)).code, 200);
  assert.equal(h.seen.runs.length, 1);
});
test('истёкший arm: 30 с без фразы, 120 с с фразой', async () => {
  const h = harness();
  const plain = (await h.arm('engine.pause')).data.arm_id;
  const reset = await h.arm('kill.reset');
  assert.equal(reset.data.expires_in_s, 120);
  assert.equal(reset.data.min_hold_ms, 2500);
  h.clock.t += 30_000;
  assert.deepEqual((await h.commit(plain)).data, { error: 'arm-expired' });
  assert.equal((await h.commit(plain)).code, 404, 'истёкший arm удалён');
  h.clock.t += 89_999;                                // 119,999 с от arm — фраза ещё действует
  assert.equal((await h.commit(reset.data.arm_id, 'СНЯТЬ KILL')).code, 200);
  const late = (await h.arm('kill.reset')).data.arm_id;
  h.clock.t += 120_000;
  assert.equal((await h.commit(late, 'СНЯТЬ KILL')).code, 410);
  assert.deepEqual(h.seen.runs, [['kill.reset', 'СНЯТЬ KILL']]);
});
test('повторное использование arm_id отклоняется — и после успеха, и после сбоя', async () => {
  const h = harness({ run: async a => ({ ok: a === 'kill.engage', action: a, summary: 'итог', outcome: a === 'kill.engage' ? 'ok' : 'failed' }) });
  const first = (await h.arm('kill.engage')).data.arm_id;
  const second = (await h.arm('engine.resume')).data.arm_id;
  h.clock.t += 2000;
  assert.equal((await h.commit(first)).code, 200);
  assert.deepEqual((await h.commit(first)).data, { error: 'unknown-arm' });
  assert.equal((await h.commit(second)).code, 502);
  assert.equal((await h.commit(second)).code, 404);
  assert.equal(h.seen.runs.length, 2);
  assert.equal((await h.commit('0'.repeat(32))).code, 404);
  assert.equal((await h.commit('../../etc')).code, 404);
});
test('чужая сессия не видит и не гасит arm', async () => {
  const h = harness();
  const { arm_id } = (await h.arm('kill.engage')).data;
  h.clock.t += 2000;
  const foreign = await h.commit(arm_id, undefined, { token: 'second-session-token' });
  assert.equal(foreign.code, 404);
  assert.equal(foreign.data.error, 'unknown-arm');
  assert.equal(h.seen.runs.length, 0);
  assert.equal((await h.commit(arm_id)).code, 200);
});
test('busy: один выполняющийся action на процесс, arm при этом не сгорает', async () => {
  const gate = deferred();
  const h = harness({ run: async a => { await gate.promise; return { ok: true, action: a, summary: 'ok', outcome: 'ok' }; } });
  const a1 = (await h.arm('kill.engage')).data.arm_id;
  const a2 = (await h.arm('orders.cancel_all')).data.arm_id;
  h.clock.t += 2000;
  const running = h.commit(a1);
  await tick();
  assert.deepEqual(h.seen.runs, [['kill.engage', null]]);
  assert.deepEqual((await h.arm('engine.pause')).data, { error: 'busy' });
  const blocked = await h.commit(a2);
  assert.equal(blocked.code, 423);
  gate.resolve();
  assert.equal((await running).code, 200);
  assert.equal((await h.commit(a2)).code, 200);
  assert.equal(h.seen.runs.length, 2);
});
test('гонка двух commit одного arm: выполняется ровно один', async () => {
  const gate = deferred();
  const h = harness({ run: async a => { await gate.promise; return { ok: true, action: a, summary: 'ok', outcome: 'ok' }; } });
  const { arm_id } = (await h.arm('kill.engage')).data;
  h.clock.t += 2000;
  const both = [h.commit(arm_id), h.commit(arm_id)];
  await tick();
  gate.resolve();
  const codes = (await Promise.all(both)).map(r => r.code).sort();
  assert.deepEqual(codes, [200, 404]);
  assert.equal(h.seen.runs.length, 1);
});
test('неверная фраза: 403, arm погашен; верная фраза уходит исполнителю', async () => {
  const h = harness();
  let id = (await h.arm('breaker.reset.daily')).data.arm_id;
  h.clock.t += 2500;
  assert.deepEqual((await h.commit(id, 'снять daily')).data, { error: 'confirm-mismatch' });
  assert.equal((await h.commit(id, 'СНЯТЬ DAILY')).code, 404);
  for (const wrong of [undefined, null, '', 'СНЯТЬ DAILY ', 'СНЯТЬ GLOBAL']) {
    id = (await h.arm('breaker.reset.daily')).data.arm_id;
    h.clock.t += 2500;
    assert.equal((await h.commit(id, wrong)).code, 403, String(wrong));
  }
  assert.equal(h.seen.runs.length, 0);
  id = (await h.arm('breaker.reset.daily')).data.arm_id;
  h.clock.t += 2500;
  const ok = await h.commit(id, 'СНЯТЬ DAILY');
  assert.equal(ok.code, 200);
  assert.deepEqual(h.seen.runs, [['breaker.reset.daily', 'СНЯТЬ DAILY']]);
});
test('недоступное действие: 409 с причиной, arm не выдаётся', async () => {
  const h = harness({ describe: () => controls({ 'kill.reset': { available: false, reason: 'kill-switch не активен' } }) });
  const response = await h.arm('kill.reset');
  assert.equal(response.code, 409);
  assert.deepEqual(response.data, { error: 'unavailable', reason: 'kill-switch не активен' });
});
test('состояние не прочитано: безопасная сторона доступна, остальное — нет', async () => {
  const h = harness({ describe: () => { throw new Error(SECRET); } });
  assert.equal((await h.arm('kill.engage')).code, 200);
  assert.equal((await h.arm('orders.cancel_all')).code, 200);
  for (const action of ['engine.pause', 'engine.resume', 'breaker.reset.global', 'kill.reset']) {
    const response = await h.arm(action);
    assert.equal(response.code, 409, action);
    assert.ok(!JSON.stringify(response.data).includes('do-not-expose'));
  }
  const live = harness({ describe: () => ({ ...controls(), mode: 'live', enabled: false }) });
  assert.equal((await live.arm('kill.engage')).code, 409, 'live — даже kill');
});
test('неизвестное действие и лишние поля: 400 invalid-action, состояние не читается', async () => {
  const h = harness();
  for (const body of [{ action: 'engine.reset' }, { action: '__proto__' }, { action: 'constructor' },
                      { action: 'toString' }, { action: 123 }, {}, { action: 'kill.engage', confirm: 'x' },
                      ['kill.engage'], 'kill.engage', null]) {
    const response = await h.post('/api/project/action/arm', body);
    assert.equal(response.code, 400, JSON.stringify(body));
    assert.equal(response.data.error, 'invalid-action');
  }
  for (const body of [{ arm_id: 1 }, { arm_id: 'a'.repeat(32), confirm: 5 }, { arm_id: 'a'.repeat(32), extra: true },
                      { arm_id: 'a'.repeat(32), confirm: 'x'.repeat(65) }]) {
    assert.equal((await h.post('/api/project/action/commit', body)).code, 400, JSON.stringify(body));
  }
  assert.equal(h.seen.described, 0);
});
test('POST: Origin обязателен и только свой, чужой Host и cross/same-site отклоняются', async () => {
  const h = harness();
  const cases = [{ origin: undefined }, { origin: 'https://untrusted.test' }, { origin: 'null' },
                 { origin: 'http://127.0.0.1:7484' }, { host: 'untrusted.test:7480' },
                 { site: 'cross-site' }, { site: 'same-site' }, { site: 'none' }];
  for (const extra of cases) {
    for (const path of ['/api/project/action/arm', '/api/project/action/commit']) {
      const response = await h.post(path, { action: 'kill.engage' }, extra);
      assert.equal(response.code, 403, `${path} ${JSON.stringify(extra)}`);
    }
  }
  assert.equal((await h.arm('kill.engage', { site: undefined })).code, 200, 'без Sec-Fetch-Site, но со своим Origin');
  assert.equal((await h.arm('kill.engage', { token: null })).code, 401);
  assert.equal(h.seen.runs.length, 0);
});
test('не-JSON: 415, тело не читается и сессия не проверяется', async () => {
  const h = harness();
  for (const type of [undefined, 'text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data', 'application/jsonp']) {
    const response = await h.arm('kill.engage', { type });
    assert.equal(response.code, 415, String(type));
    assert.equal(response.data.error, 'json-required');
  }
  assert.equal(h.seen.validated, 0);
  assert.equal((await h.arm('kill.engage', { type: 'application/json; charset=utf-8' })).code, 200);
});
test('большое тело и длина: 413/411, поток без парсера, ошибки express.json без текста', async () => {
  const h = harness();
  assert.deepEqual((await h.arm('kill.engage', { length: '2049' })).data, { error: 'body-too-large' });
  assert.equal((await h.arm('kill.engage', { length: '99999999999' })).code, 400);
  assert.equal((await h.arm('kill.engage', { length: undefined })).code, 411);
  const padded = { action: 'kill.engage', pad: 'x'.repeat(4000) };
  assert.equal((await h.post('/api/project/action/arm', padded, { length: '20' })).code, 413, 'Content-Length занижен');
  assert.equal(h.seen.described, 0);
  const raw = '{"action":"kill.engage"}';
  assert.equal((await h.arm(null, { body: undefined, raw, length: String(raw.length) })).code, 200);
  assert.deepEqual((await h.arm(null, { body: undefined, raw: '{"action":', length: '10' })).data, { error: 'invalid-json' });
  const handler = h.errorHandler();
  for (const [err, code, error] of [[{ type: 'entity.parse.failed', status: 400, message: SECRET }, 400, 'invalid-json'],
                                    [{ type: 'entity.too.large', status: 413, message: SECRET }, 413, 'body-too-large'],
                                    [{ type: 'charset.unsupported', status: 415, message: SECRET }, 415, 'json-required'],
                                    [new Error(SECRET), 500, 'internal-error']]) {
    const res = { code: 0, data: null, headersSent: false, set() { return this; },
                  status(n) { this.code = n; return this; }, json(d) { this.data = d; return this; } };
    handler(err, {}, res, () => assert.fail('next не вызывается'));
    assert.equal(res.code, code);
    assert.deepEqual(res.data, { error });
  }
});
test('commit сбрасывает кеш /state и отбрасывает сбор, начатый до действия', async () => {
  let gate = null;
  let version = 0;
  const h = harness({ collect: async () => { const v = ++version; if (gate) await gate.promise; return { schema_version: 1, v }; } });
  assert.equal((await h.request()).data.v, 1);
  assert.equal((await h.request()).data.v, 1);
  let { arm_id } = (await h.arm('kill.engage')).data;
  h.clock.t += 2000;
  assert.equal((await h.commit(arm_id)).code, 200);
  assert.equal((await h.request()).data.v, 2, 'после commit — свежий сбор');
  h.clock.t += 10_000;
  gate = deferred();
  const before = h.request();                          // сбор v3 начат до действия
  await tick();
  ({ arm_id } = (await h.arm('orders.cancel_all')).data);
  h.clock.t += 2000;
  assert.equal((await h.commit(arm_id)).code, 200);
  const after = h.request();                           // не ждёт старый сбор, а начинает v4
  await tick();
  gate.resolve();
  assert.equal((await before).data.v, 3);
  assert.equal((await after).data.v, 4);
  assert.equal((await h.request()).data.v, 4, 'в кеше только сбор после действия');
});
test('нет утечки stdout/stderr/output и текста исключений в ответах', async () => {
  const leaky = harness({ run: async a => ({ ok: true, action: a, summary: 'Готово', outcome: 'ok', stdout: SECRET,
                                             stderr: SECRET, output: SECRET, token: SECRET,
                                             details: { cancelled: 2, output: SECRET, note: SECRET, scope: 'daily' } }) });
  let id = (await leaky.arm('kill.engage')).data.arm_id;
  leaky.clock.t += 2000;
  const ok = await leaky.commit(id);
  assert.equal(ok.code, 200);
  assert.deepEqual(ok.data, { ok: true, action: 'kill.engage', summary: 'Готово', outcome: 'ok',
                              details: { cancelled: 2, scope: 'daily' } });
  const throwing = harness({ run: async () => { const e = new Error(SECRET); e.stdout = SECRET; e.stderr = SECRET; throw e; } });
  id = (await throwing.arm('kill.engage')).data.arm_id;
  throwing.clock.t += 2000;
  const failed = await throwing.commit(id);
  assert.equal(failed.code, 502);
  assert.equal(failed.data.error, 'action-failed');
  assert.ok(!JSON.stringify(failed.data).includes('do-not-expose'));
  const spoofed = harness({ run: async () => ({ ok: true, action: 'kill.reset', summary: SECRET }) });
  id = (await spoofed.arm('kill.engage')).data.arm_id;
  spoofed.clock.t += 2000;
  const mismatch = await spoofed.commit(id);
  assert.equal(mismatch.code, 502, 'чужой action в ответе исполнителя — не успех');
  assert.ok(!JSON.stringify(mismatch.data).includes('do-not-expose'));
});
test('настоящий запуск Python: execFile без shell, фиксированные аргументы, фраза только в env', async () => {
  const calls = [];
  let reply = async () => ({ stdout: `шум ${SECRET}\n{"ok":true,"action":"kill.engage","summary":"Kill-switch включён","outcome":"ok","output":"${SECRET}"}\n` });
  const exec = async (file, args, opts) => { calls.push({ file, args, opts }); return reply(file, args, opts); };
  const h = harness({ run: null, exec });
  let id = (await h.arm('kill.engage')).data.arm_id;
  h.clock.t += 2000;
  const ok = await h.commit(id);
  assert.equal(ok.code, 200);
  assert.deepEqual(ok.data, { ok: true, action: 'kill.engage', summary: 'Kill-switch включён', outcome: 'ok' });
  const call = calls[0];
  assert.match(call.file, /python\.exe$/);
  assert.deepEqual(call.args, ['-m', 'src.morphy_actions', 'run', 'kill.engage']);
  assert.equal(call.opts.shell, false);
  assert.equal(call.opts.timeout, 120_000);
  assert.equal(call.opts.env[CONFIRM_ENV], undefined);

  reply = async (_f, args) => ({ stdout: `{"ok":true,"action":"${args[3]}","summary":"Снято","outcome":"ok","details":{"scope":"kill"}}` });
  id = (await h.arm('kill.reset')).data.arm_id;
  h.clock.t += 2500;
  assert.equal((await h.commit(id, 'СНЯТЬ KILL')).code, 200);
  assert.deepEqual(calls[1].args, ['-m', 'src.morphy_actions', 'run', 'kill.reset']);
  assert.equal(calls[1].opts.env[CONFIRM_ENV], 'СНЯТЬ KILL');
  assert.ok(!calls[1].args.some(arg => arg.includes('СНЯТЬ')), 'фраза не в argv');

  reply = async () => { const e = new Error(SECRET); e.code = 1; e.stderr = SECRET;
    e.stdout = '{"ok":false,"action":"engine.pause","summary":"Движок уже остановлен","outcome":"rejected"}'; throw e; };
  id = (await h.arm('engine.pause')).data.arm_id;
  h.clock.t += 2000;
  const rejected = await h.commit(id);
  assert.equal(rejected.code, 502);
  assert.deepEqual(rejected.data, { error: 'action-failed', ok: false, action: 'engine.pause',
                                    summary: 'Движок уже остановлен', outcome: 'rejected' });

  reply = async () => { const e = new Error(SECRET); e.killed = true; e.signal = 'SIGTERM'; e.stdout = `${SECRET}`; throw e; };
  id = (await h.arm('orders.cancel_all')).data.arm_id;
  h.clock.t += 2000;
  const timeout = await h.commit(id);
  assert.equal(timeout.code, 502);
  assert.match(timeout.data.summary, /таймауту 120 с/);
  assert.ok(!JSON.stringify(timeout.data).includes('do-not-expose'));
});
test('настоящий describe: фиксированные аргументы, без фразы в окружении', async () => {
  const calls = [];
  const exec = async (file, args, opts) => { calls.push({ args, opts }); return { stdout: JSON.stringify(controls()) }; };
  const h = harness({ describe: null, exec });
  assert.equal((await h.arm('engine.pause')).code, 200);
  assert.deepEqual(calls[0].args, ['-m', 'src.morphy_actions', 'describe']);
  assert.equal(calls[0].opts.shell, false);
  assert.equal(calls[0].opts.env[CONFIRM_ENV], undefined);
});
test('повторы arm не копят состояние: не больше 4 arm на сессию', async () => {
  const h = harness();
  const ids = [];
  for (let i = 0; i < 6; i++) ids.push((await h.arm('kill.engage')).data.arm_id);
  h.clock.t += 2000;
  assert.equal((await h.commit(ids[0])).code, 404, 'старейший вытеснен');
  assert.equal((await h.commit(ids[1])).code, 404);
  assert.equal((await h.commit(ids[5])).code, 200);
});
test('разбор вывода исполнителя: только последняя строка JSON', () => {
  assert.equal(parseActionOutput(undefined), null);
  assert.equal(parseActionOutput(''), null);
  assert.equal(parseActionOutput('{"ok":true}\nне json'), null);
  assert.equal(parseActionOutput('["ok"]'), null);
  assert.deepEqual(parseActionOutput('лог\n{"ok":false,"action":"kill.reset"}\n'), { ok: false, action: 'kill.reset' });
});
test('таблица действий сервера — зеркало контракта', () => {
  assert.deepEqual(Object.keys(ACTIONS), ['kill.engage', 'orders.cancel_all', 'engine.pause', 'engine.resume',
                                         'breaker.reset.daily', 'breaker.reset.global', 'kill.reset']);
  assert.deepEqual(Object.values(ACTIONS).map(a => a.holdMs), [2000, 2000, 2000, 2000, 2500, 2500, 2500]);
  assert.deepEqual(Object.values(ACTIONS).map(a => a.phrase),
                   [null, null, null, null, 'СНЯТЬ DAILY', 'СНЯТЬ GLOBAL', 'СНЯТЬ KILL']);
  assert.ok(Object.isFrozen(ACTIONS));
  assert.match(CONFIRM_ENV, /RESET_BREAKER/, 'имя попадает под правило guard reset_breaker');
});
