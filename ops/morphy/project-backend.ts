// Роуты локального приложения Morphy. Известные read-only команды, без shell.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';

const execFileAsync = promisify(execFile);
type Options = {
  validate?: (token: string) => Promise<boolean>;
  collect?: (task?: string) => Promise<any>;
};

export function mountProjectRoutes(app: any, options: Options = {}) {
  // deployed file: <project>/data/morphy-eval/package/workspace/backend/project.ts
  const project = path.resolve(import.meta.dirname, '../../../../..');
  const python = path.join(project, '.venv', 'Scripts', 'python.exe');
  const supervisorPort = Number(process.env.SUPERVISOR_PORT || 7480);
  const allowedHosts = new Set([`127.0.0.1:${supervisorPort}`, `localhost:${supervisorPort}`,
                               `127.0.0.1:${supervisorPort + 4}`, `localhost:${supervisorPort + 4}`]);
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
    const args = ['-m', 'src.morphy_project', ...(task ? ['--task', task] : [])];
    const { stdout } = await execFileAsync(python, args, {
      cwd: project, windowsHide: true, timeout: 12000, maxBuffer: 2 * 1024 * 1024,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    });
    return JSON.parse(stdout);
  });
  let cache: { at: number; data: any } | null = null;
  let pending: Promise<any> | null = null;

  app.use('/api/project', async (req: any, res: any, next: any) => {
    res.set('Cache-Control', 'no-store');
    if (!allowedHosts.has(req.headers.host) || req.headers['sec-fetch-site'] === 'cross-site') {
      res.status(403).json({ error: 'local-origin-required' }); return;
    }
    const origin = req.headers.origin;
    if (origin && origin !== `http://127.0.0.1:${supervisorPort}` && origin !== `http://localhost:${supervisorPort}`) {
      res.status(403).json({ error: 'local-origin-required' }); return;
    }
    if (req.method !== 'GET') { res.status(405).json({ error: 'read-only' }); return; }
    const match = /^Bearer (\S{1,4096})$/.exec(req.headers.authorization || '');
    if (!match || !await validate(match[1])) {
      res.status(401).json({ error: 'morphy-login-required' }); return;
    }
    next();
  });
  app.get('/api/project/state', async (_req: any, res: any) => {
    try {
      if (cache && Date.now() - cache.at < 10000) { res.json(cache.data); return; }
      if (!pending) pending = collect().then(data => {
        if (data.schema_version !== 1) throw new Error('Unsupported schema');
        cache = { at: Date.now(), data }; return data;
      }).finally(() => { pending = null; });
      res.json(await pending);
    } catch { res.status(503).json({ error: 'project-data-unavailable' }); }
  });
  app.get('/api/project/task/:id', async (req: any, res: any) => {
    if (!/^[A-Z0-9_-]{1,80}$/.test(req.params.id)) { res.status(400).json({ error: 'invalid-task-id' }); return; }
    try { res.json(await collect(req.params.id)); }
    catch { res.status(404).json({ error: 'task-unavailable' }); }
  });
}
