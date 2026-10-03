import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowUpRight, Bot, CheckCircle2, ChevronRight, Clock3, FileText, GitBranch, Layers, Link2, ListTodo, RefreshCw, Search, ShieldCheck, X } from 'lucide-react';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { useLocation } from 'react-router';
import './project.css';

const titles: Record<string, string> = { ready: 'Готово к работе', 'in-progress': 'В работе', done: 'Выполнено', blocked: 'Заблокировано', 'needs-user': 'Нужно решение', scheduled: 'По расписанию' };
const tabs = [{ id: 'overview', name: 'Обзор', icon: Activity }, { id: 'tasks', name: 'Задачи', icon: ListTodo }, { id: 'agents', name: 'Агенты', icon: Bot }, { id: 'integrations', name: 'Интеграции', icon: Link2 }, { id: 'journal', name: 'Знания и события', icon: FileText }];
const number = (n: any, digits = 2) => typeof n === 'number' ? n.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
const time = (s: any) => s ? new Date(typeof s === 'number' ? s * 1000 : s).toLocaleString('ru-RU', { timeZone: 'Asia/Qyzylorda', hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' }) : '—';
const day = (s: any) => s ? new Date(typeof s === 'number' ? s * 1000 : s).toLocaleString('ru-RU', { timeZone: 'Asia/Qyzylorda', day: '2-digit', month: '2-digit' }) : '—';
const integrationNames: Record<string, string> = { observed: 'Данные доступны', online: 'Онлайн', offline: 'Офлайн', paused: 'На паузе', 'files-present': 'Файлы доступны', 'cli-present': 'CLI установлен', 'needs-setup': 'Нужна настройка', 'board-complete': 'Настройка отмечена', unavailable: 'Нет данных' };

async function api(path: string) {
  const token = localStorage.getItem('bloby_token');
  const response = await fetch('/app/api/project/' + path, { headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(response.status === 401 ? 'login' : response.status === 404 ? 'not-found' : 'unavailable');
  return response.json();
}

function Badge({ status, text }: { status: string; text?: string }) { return <span className={`pj-badge pj-${status}`}>{text || titles[status] || status}</span>; }
function Metric({ label, value, hint, tone = '' }: any) { return <div className={`pj-metric ${tone}`}><span>{label}</span><strong>{value}</strong><small>{hint}</small></div>; }
function Empty({ children }: any) { return <div className="pj-empty">{children}</div>; }
function TaskList({ tasks, open, ready }: any) {
  return tasks.length ? <div className="pj-task-list">{tasks.map((task: any) => <button className="pj-task" key={task.id + ':' + task.line} onClick={() => open(task.id)}>
    <div className="pj-task-main"><span className="pj-id">{task.id}</span><strong>{task.title}</strong><small>{task.agent}</small></div>
    <div className="pj-task-status"><Badge status={task.status} />{task.status === 'ready' && !ready?.includes(task.id) && <small>Ожидает зависимости</small>}</div><ChevronRight size={16} />
  </button>)}</div> : <Empty>Задач по этому фильтру нет</Empty>;
}

export default function ProjectDashboard() {
  const location = useLocation();
  const [data, setData] = useState<any>(null);
  const [tab, setTab] = useState('overview');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('active');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [task, setTask] = useState<any>(null);
  const [taskError, setTaskError] = useState('');
  const [copied, setCopied] = useState(false);
  const refresh = useCallback(async () => {
    setLoading(true);
    try { setData(await api('state')); setError(''); }
    catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { refresh(); const timer = setInterval(refresh, 15000); return () => clearInterval(timer); }, [refresh]);
  useEffect(() => { const section = new URLSearchParams(location.search).get('section'); if (section && tabs.some(item => item.id === section)) setTab(section); }, [location.search]);
  useEffect(() => { const listener = (event: KeyboardEvent) => { if (event.key === 'Escape') { setTask(null); setTaskError(''); } }; window.addEventListener('keydown', listener); return () => window.removeEventListener('keydown', listener); }, []);
  const openTask = async (id: string) => {
    setTask({ id, loading: true }); setTaskError(''); setCopied(false);
    try { setTask(await api('task/' + encodeURIComponent(id))); }
    catch { setTask(null); setTaskError('Карточка недоступна. Возможно, ID дублируется или данные изменились.'); }
  };
  const copyTask = async () => {
    try { await navigator.clipboard.writeText(`Задача ${task.id}: ${task.title}\nСтатус: ${task.status}\nАгент: ${task.agent}\nЗависимости: ${task.deps?.join(', ') || '—'}\n\nКритерий готовности\n${task.criterion}\n\nЗаметки\n${task.notes}`); setCopied(true); }
    catch { setTaskError('Не удалось скопировать. Выдели текст карточки вручную.'); }
  };
  const tasks = data?.board?.tasks || [];
  const filtered = tasks.filter((t: any) => (status === 'all' || status === 'active' && t.status !== 'done' || t.status === status) && `${t.id} ${t.title} ${t.agent}`.toLowerCase().includes(search.toLowerCase()));
  const needsUser = tasks.filter((t: any) => t.status === 'needs-user');
  const risk = data?.risk || {};
  const engine = data?.engine || {};
  const lifetime = data?.lifetime || {};
  const chart = (data?.equity_history || []).filter((p: any) => Array.isArray(p) && typeof p[1] === 'number').map((p: any) => ({ ts: p[0], equity: p[1] }));
  const blocked = risk.kill_active || risk.daily_breaker || risk.global_breaker;
  const age = data ? (Date.now() - Date.parse(data.generated_at)) / 1000 : 0;

  return <div className="pj-root">
    <header className="pj-header"><div><div className="pj-eyebrow"><span className="pj-dot" /> OKX · РАБОЧЕЕ ПРОСТРАНСТВО</div><h1>Проект под контролем</h1><p>Задачи, агенты и состояние торговли в одном месте.</p></div>
      <div className="pj-header-actions"><span className="pj-demo">DEMO</span><button className="pj-icon-button" onClick={refresh} aria-label="Обновить данные" title="Обновить данные" disabled={loading}><RefreshCw size={18} className={loading ? 'pj-spin' : ''} /></button></div></header>
    <nav className="pj-tabs" aria-label="Разделы проекта">{tabs.map(({ id, name, icon: Icon }) => <button key={id} aria-current={tab === id ? 'page' : undefined} className={tab === id ? 'selected' : ''} onClick={() => setTab(id)}><Icon size={17} />{name}{id === 'tasks' && needsUser.length > 0 && <span className="pj-tab-count">{needsUser.length}</span>}</button>)}</nav>
    {error === 'login' ? <div className="pj-notice"><ShieldCheck size={20} /><div><strong>Войди в Morphy</strong><p>Экран проекта использует существующий вход и пароль Morphy.</p><a href="/bloby" target="_top">Открыть вход в Morphy <ArrowUpRight size={14} /></a></div></div> : error && <div role="alert" className="pj-alert">Обновление не удалось. {data ? 'Показан последний полученный снимок.' : 'Проверь доступность Morphy и повтори обновление.'}</div>}
    {age > 60 && <div role="alert" className="pj-alert">Снимок старше минуты. Последние данные: {time(data.generated_at)}.</div>}
    {taskError && <div role="alert" className="pj-alert">{taskError}</div>}
    {!data && !error && <Empty>Загружаю данные проекта…</Empty>}
    {data && <>
      {tab === 'overview' && <>
        <div className="pj-metrics"><Metric label="Капитал demo" value={`${number(risk.equity)} USDT`} hint={risk.equity_age_s == null ? 'Нет свежести источника' : `Обновление источника ${number(risk.equity_age_s, 0)} сек. назад`} />
          <Metric label="Результат дня" value={`${risk.day_pnl > 0 ? '+' : ''}${number(risk.day_pnl)} USDT`} hint={`${number(risk.day_pnl_pct)}% за день`} tone={risk.day_pnl < 0 ? 'negative' : 'positive'} />
          <Metric label="Прибыль за всё время" value={lifetime.available ? `${lifetime.net_usdt > 0 ? '+' : ''}${number(lifetime.net_usdt)} USDT` : '—'}
            hint={lifetime.available ? `с ${day(lifetime.since)} · ${lifetime.net_pct == null ? '' : `${lifetime.net_pct > 0 ? '+' : ''}${number(lifetime.net_pct)}% · `}пополнения demo исключены` : 'Нет истории капитала'}
            tone={lifetime.available ? (lifetime.net_usdt < 0 ? 'negative' : 'positive') : ''} />
          <Metric label="Просадка" value={`${number(typeof risk.drawdown_pct === 'number' ? Math.abs(risk.drawdown_pct) : null)}%`} hint={`Лимит ${number(risk.global_dd_limit_pct)}%`} />
          <Metric label="Движок" value={engine.running === true ? 'Работает' : engine.running === false ? 'Остановлен' : 'Нет данных'} hint={`${number(engine.uptime_h, 1)} ч. работы · ${number(engine.reconciles, 0)} сверок`} tone={engine.running ? 'positive' : 'negative'} /></div>
        <div className="pj-overview-grid"><section className="pj-panel"><div className="pj-section-head"><div><h2>Кривая капитала</h2><p>Локальная история за последние 48 часов</p></div><span className="pj-small-label">USDT</span></div>
          {chart.length > 1 ? <div className="pj-chart"><ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{ width: 300, height: 235 }}><AreaChart data={chart} margin={{ top: 12, right: 8, left: 4, bottom: 0 }}><defs><linearGradient id="pj-equity" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#31c8a4" stopOpacity={0.28} /><stop offset="100%" stopColor="#31c8a4" stopOpacity={0} /></linearGradient></defs><CartesianGrid stroke="#ffffff0c" vertical={false} /><XAxis dataKey="ts" tickFormatter={v => time(v)} tick={{ fill: '#8d98a6', fontSize: 10 }} minTickGap={60} axisLine={false} tickLine={false} /><YAxis domain={['auto', 'auto']} tickFormatter={v => number(v, 0)} tick={{ fill: '#8d98a6', fontSize: 10 }} width={65} axisLine={false} tickLine={false} /><Tooltip contentStyle={{ background: '#20272d', border: '1px solid #38434d', borderRadius: 12 }} labelFormatter={v => time(v)} formatter={(v: any) => [`${number(v)} USDT`, 'Капитал']} /><Area type="monotone" dataKey="equity" stroke="#31c8a4" strokeWidth={2} fill="url(#pj-equity)" isAnimationActive={false} /></AreaChart></ResponsiveContainer></div> : <Empty>Истории для графика пока недостаточно</Empty>}
        </section><section className="pj-panel"><div className="pj-section-head"><div><h2>Безопасность</h2><p>Состояние существующего риск-ядра</p></div><ShieldCheck size={20} /></div>
          <div className={`pj-safety ${blocked ? 'warning' : ''}`}><CheckCircle2 size={20} /><div><strong>{blocked ? 'Входы заблокированы' : risk.kill_active == null ? 'Нет данных риска' : 'Блокировки не активны'}</strong><small>Kill-switch и breaker’ы</small></div></div>
          <div className="pj-risk-line"><span>Риск позиций</span><strong>{number(risk.portfolio_heat_pct)} / {number(risk.max_heat_pct)}%</strong></div><div className="pj-progress"><i style={{ width: `${Math.min(100, Math.max(0, (risk.portfolio_heat_pct || 0) / (risk.max_heat_pct || 1) * 100))}%` }} /></div>
          <div className="pj-risk-line"><span>Входов за день</span><strong>{number(risk.entries_today, 0)} / {number(risk.max_entries_per_day, 0)}</strong></div><div className="pj-risk-line"><span>Внутренние ошибки</span><strong>{number(engine.errors_internal, 0)}</strong></div><div className="pj-risk-line"><span>Ошибки биржи</span><strong>{number(engine.errors_exchange, 0)}</strong></div><div className="pj-risk-line"><span>Отказы guard за 24 ч.</span><strong>{number(data.ops?.guard?.denies_24h, 0)}</strong></div>
        </section></div>
        <div className="pj-overview-grid"><section className="pj-panel"><div className="pj-section-head"><div><h2>Нужно твоё решение <span className="pj-inline-count">{needsUser.length}</span></h2><p>Вопросы из единой доски проекта</p></div><button className="pj-text-button" onClick={() => { setStatus('needs-user'); setTab('tasks'); }}>Все вопросы <ArrowUpRight size={14} /></button></div><TaskList tasks={needsUser.slice(0, 4)} open={openTask} ready={data.board.ready_ids} /></section>
          <section className="pj-panel"><div className="pj-section-head"><div><h2>Работа команды</h2><p>Claims доски — не подтверждение живой сессии</p></div><Bot size={20} /></div><TaskList tasks={data.claims.slice(0, 4)} open={openTask} ready={data.board.ready_ids} /></section></div>
      </>}
      {tab === 'tasks' && <section className="pj-panel"><div className="pj-section-head"><div><h2>Единая доска задач</h2><p>{tasks.length} задач · {data.board.ready_ids.length} доступны с учётом зависимостей</p></div></div><div className="pj-filters"><label className="pj-search"><Search size={17} /><input aria-label="Поиск задач" placeholder="Найти задачу, ID или агента" value={search} onChange={e => setSearch(e.target.value)} /></label><select aria-label="Фильтр статуса" value={status} onChange={e => setStatus(e.target.value)}><option value="active">Все активные</option><option value="all">Все, включая выполненные</option>{Object.entries(titles).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></div>{data.board.duplicates.length > 0 && <div className="pj-alert">Дубли ID: {data.board.duplicates.join(', ')}. Карточки требуют сверки.</div>}<TaskList tasks={filtered} open={openTask} ready={data.board.ready_ids} /></section>}
      {tab === 'agents' && <><div className="pj-runtime-grid">{data.runtimes.map((runtime: any) => <section className="pj-panel pj-runtime" key={runtime.id}><div className="pj-runtime-icon"><Bot size={22} /></div><h2>{runtime.id === 'claude' ? 'Claude Code' : runtime.id === 'gemini' ? 'Gemini' : runtime.id === 'codex' ? 'Codex' : 'Muse Code'}</h2><p>{runtime.model || 'Модель не указана'}</p><Badge status={runtime.cli_available ? 'done' : 'blocked'} text={runtime.cli_available ? 'CLI установлен' : 'CLI не найден'} /><small>Guard в реестре: {runtime.guard_status}</small></section>)}</div><section className="pj-panel"><div className="pj-section-head"><div><h2>Текущие задачи агентов</h2><p>Данные доски; работа рантайма не выводится из одного claim</p></div></div><TaskList tasks={data.claims} open={openTask} ready={data.board.ready_ids} /></section><section className="pj-panel"><div className="pj-section-head"><div><h2>Очередь Muse</h2><p>{data.queue.paused ? 'Очередь на паузе' : 'Наблюдение файловой очереди'} · стоимость вызовов неизвестна</p></div></div>{data.queue.jobs.length ? <div className="pj-queue">{data.queue.jobs.slice(0, 12).map((job: any) => <div key={job.id}><span className="pj-id">{job.id}</span><span>{job.role || 'Роль не указана'}</span><Badge status={job.state} text={job.state === 'processing-unverified' ? 'В обработке · не проверено' : job.state} /><small>{time(job.created)}</small></div>)}</div> : <Empty>Заявок в очереди нет</Empty>}</section></>}
      {tab === 'integrations' && <><div className="pj-section-head"><div><h2>Интеграции проекта</h2><p>Состояние подключений и источников данных</p></div></div><div className="pj-integration-grid">{data.integrations.map((item: any) => <section className="pj-panel pj-integration" key={item.id}><div className="pj-integration-top"><span className="pj-integration-icon"><Link2 size={20} /></span><small>{item.group}</small></div><h3>{item.name}</h3><p>{item.detail}</p><Badge status={item.status} text={integrationNames[item.status] || item.status} /><small className="pj-source">Источник: {item.source}</small>{item.url && <a href={item.url} target="_blank" rel="noreferrer">Открыть панель <ArrowUpRight size={14} /></a>}</section>)}</div><section className="pj-panel"><div className="pj-section-head"><div><h2>Модули и инфраструктура</h2><p>Наличие кода не означает запуск стратегии или выполнение внешней операции</p></div><Layers size={20} /></div><div className="pj-module-grid">{data.modules.map((item: any) => <div className="pj-module" key={item.id}><strong>{item.name}</strong><p>{item.description}</p><Badge status={item.present ? 'done' : 'blocked'} text={item.present ? 'Код доступен' : 'Часть файлов отсутствует'} /><small>{item.files.join(' · ')}</small></div>)}</div></section><section className="pj-panel"><div className="pj-section-head"><div><h2>Git и GitHub</h2><p>Состояние рабочего репозитория</p></div><GitBranch size={20} /></div><div className="pj-git"><span>Ветка <strong>{data.git.branch || '—'}</strong></span><span>Commit <strong>{data.git.commit || '—'}</strong></span><span>Изменено файлов <strong>{number(data.git.changed_files, 0)}</strong></span><span>Remote <strong>{number(data.git.remote_count, 0)}</strong></span><span>GitHub CLI <strong>{data.git.github_cli_available ? 'Установлен' : 'Не найден'}</strong></span></div><p className="pj-muted">Доступ к GitHub и синхронизация не проверялись. Изменения репозитория не отправляются.</p></section></>}
      {tab === 'journal' && <div className="pj-overview-grid"><section className="pj-panel"><div className="pj-section-head"><div><h2>Исследования и отчёты</h2><p>Последние обновления базы проекта</p></div></div>{data.insights.map((item: any) => <div className="pj-document" key={item.path}><FileText size={18} /><div><strong>{item.title}</strong><p>{item.status || 'Статус не указан'}</p><small>{item.path} · {time(item.updated_at)}</small></div></div>)}</section><section className="pj-panel"><div className="pj-section-head"><div><h2>Инциденты</h2><p>Заголовки журнала; исходные логи не передаются</p></div></div>{data.incidents.length ? data.incidents.map((item: any, i: number) => <div className="pj-document" key={i}><Clock3 size={17} /><div><strong>{item.title}</strong><small>{item.source}</small></div></div>) : <Empty>Записей пока нет</Empty>}<div className="pj-section-head pj-subhead"><div><h2>Решения из Obsidian</h2><p>{data.pending_decisions.length} новых ответов ожидают переноса на доску</p></div></div>{data.pending_decisions.map((path: string) => <div className="pj-document" key={path}><FileText size={16} /><small>{path}</small></div>)}</section></div>}
      <footer className="pj-footer"><span><span className="pj-dot" /> Обновлено {time(data.generated_at)} · каждые 15 сек.</span><span>Снимок локального состояния{data.errors.length > 0 ? ` · ${data.errors.length} источников недоступно` : ''}</span></footer>
    </>}
    {task && <div className="pj-modal-backdrop" onClick={() => setTask(null)}><section className="pj-modal" role="dialog" aria-modal="true" aria-labelledby="pj-task-title" onClick={event => event.stopPropagation()}><button className="pj-modal-close pj-icon-button" aria-label="Закрыть карточку" onClick={() => setTask(null)}><X size={18} /></button><span className="pj-id">{task.id}</span><h2 id="pj-task-title">{task.loading ? 'Загружаю карточку…' : task.title}</h2>{!task.loading && <><div className="pj-modal-meta"><Badge status={task.status} /><span>{task.agent}</span></div><h3>Критерий готовности</h3><p className="pj-prose">{task.criterion || 'Не указан'}</p><h3>Зависимости</h3><p>{task.deps?.join(', ') || 'Нет зависимостей'}</p><h3>Заметки и решения</h3><p className="pj-prose">{task.notes || 'Заметок нет'}</p><div className="pj-modal-actions"><button className="pj-primary-button" onClick={copyTask}>{copied ? 'Скопировано' : 'Скопировать карточку'}</button><small>Источник: ops/board.md, строка {task.line}</small></div></>}</section></div>}
  </div>;
}
