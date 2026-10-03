// Проверка границы API без аккаунта/сессии человека и без торгового движка.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mountProjectRoutes } from './project-backend.ts';

function harness({ valid = true, collect = async () => ({ schema_version: 1, board: {} }) } = {}) {
  let middleware;
  const routes = {};
  let collected = 0;
  mountProjectRoutes({
    use: (_path, fn) => { middleware = fn; },
    get: (path, fn) => { routes[path] = fn; },
  }, { validate: async token => valid && token === 'test-only-token', collect: async task => { collected++; return collect(task); } });
  return {
    request: async ({ path = '/api/project/state', method = 'GET', host = '127.0.0.1:7480', token = 'test-only-token', origin, site, id = 'T1' } = {}) => {
      const req = { method, headers: { host, authorization: token ? `Bearer ${token}` : undefined, origin, 'sec-fetch-site': site }, params: { id } };
      const res = { code: 200, data: null, set() {}, status(n) { this.code = n; return this; }, json(data) { this.data = data; return this; } };
      let passed = false;
      await middleware(req, res, () => { passed = true; });
      if (passed) await routes[path](req, res);
      return res;
    },
    count: () => collected,
  };
}

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
test('операции записи запрещены', async () => {
  const h = harness();
  assert.equal((await h.request({ method: 'POST' })).code, 405);
  assert.equal(h.count(), 0);
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
  const h = harness({ collect: async () => { throw new Error('private-key=do-not-expose'); } });
  const response = await h.request();
  assert.equal(response.code, 503);
  assert.equal(response.data.error, 'project-data-unavailable');
  assert.ok(!JSON.stringify(response).includes('do-not-expose'));
});
