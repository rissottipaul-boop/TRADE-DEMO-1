import { useEffect, useRef, useState } from 'react';
import { Copy, RefreshCw } from 'lucide-react';
import { readToken } from './api';
import './agent-tools.css';

type Task = { id: string; title: string };
type Run = { id: string; task_id?: string; status?: string; runtime?: string };
type Props = { tasks: Task[] };

async function callTool(name: string, arguments_: Record<string, unknown> = {}, signal?: AbortSignal) {
  const token = readToken();
  if (!token) throw new Error('Войдите в Morphy, чтобы читать данные проекта.');
  const response = await fetch('/app/api/project/ai/tool', {
    method: 'POST', signal, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, arguments: arguments_ }),
  });
  const reply = await response.json();
  if (!response.ok || !reply.ok) throw new Error(response.status === 401 ? 'Сессия Morphy истекла.'
    : response.status === 400 ? 'Проверьте параметры запроса и принадлежность запуска задаче.'
    : 'Не удалось прочитать данные. Повторите обновление.');
  return reply.result;
}

const packetText = (value: any): string => typeof value === 'string' ? value : value?.markdown || JSON.stringify(value, null, 2);

export default function AgentToolsPanel({ tasks }: Props) {
  const [description, setDescription] = useState('');
  const [taskId, setTaskId] = useState('');
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunId] = useState('');
  const [usage, setUsage] = useState<any>(null);
  const [result, setResult] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [events, setEvents] = useState<any[]>([]);
  const [streamError, setStreamError] = useState('');
  const cursor = useRef(0);
  const requestSequence = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function refresh() {
      if (pending) return;
      pending = true;
      try {
        const [meter, list] = await Promise.all([callTool('ai.usage', {}, controller.signal), callTool('runs.list', { limit: 50 }, controller.signal)]);
        if (!controller.signal.aborted) { setUsage(meter); setRuns(list.runs || []); }
      } catch (err) { if (!controller.signal.aborted) setStreamError((err as Error).message); }
      finally { pending = false; }
    }
    void refresh();
    const timer = setInterval(refresh, 10000);
    return () => { clearInterval(timer); controller.abort(); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    cursor.current = 0; setEvents([]); setStreamError('');
    if (!runId) return () => controller.abort();
    let pending = false;
    async function poll() {
      if (pending) return;
      pending = true;
      try {
        const packet = await callTool('run.events', { run_id: runId, after: cursor.current, limit: 100 }, controller.signal);
        if (controller.signal.aborted) return;
        cursor.current = Math.max(cursor.current, Number(packet.next_cursor || 0));
        setEvents(previous => {
          const unique = new Map(previous.map(event => [event.cursor, event]));
          for (const event of packet.events || []) unique.set(event.cursor, event);
          return [...unique.values()].slice(-200);
        });
        setStreamError('');
      } catch (err) { if (!controller.signal.aborted) setStreamError((err as Error).message); }
      finally { pending = false; }
    }
    void poll();
    const timer = setInterval(poll, 3000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [runId]);

  async function request(name: string, arguments_: Record<string, unknown>) {
    const sequence = ++requestSequence.current;
    setBusy(true); setError(''); setResult('');
    try {
      const reply = await callTool(name, arguments_);
      if (sequence === requestSequence.current) setResult(packetText(reply));
    } catch (err) { if (sequence === requestSequence.current) setError((err as Error).message); }
    finally { if (sequence === requestSequence.current) setBusy(false); }
  }

  const totals = usage?.totals || {};
  const selectedRun = runs.find(run => run.id === runId);
  const taskArguments = { task_id: taskId, ...(runId && selectedRun?.task_id === taskId ? { run_id: runId } : {}) };
  return <section className="pj-panel pj-agent-tools">
    <div className="pj-section-head"><div><h2>Задачи, запуски и качество AI</h2><p>Инструменты помощника · только чтение · единая доска проекта</p></div></div>
    <div className="pj-agent-meters">
      <span>Подтверждённый расход <strong>{totals.reported_usd == null ? 'Нет данных' : `${Number(totals.reported_usd).toFixed(4)} USD`}</strong></span>
      <span>Без источника стоимости <strong>{totals.unknown_cost_runs ?? totals.unknown_runs ?? '—'}</strong></span>
      <span>Действия помощника <strong>{totals.recorded_actions ?? totals.actions ?? usage?.actions?.length ?? '—'}</strong></span>
    </div>
    {(usage?.alerts || []).map((alert: any, index: number) => <p className="pj-agent-warning" key={index}>{alert.message || alert.summary || alert.code} {alert.recovery || alert.next_step}</p>)}
    <label htmlFor="agent-task-description">Описание проблемы</label>
    <textarea id="agent-task-description" rows={3} maxLength={4000} value={description} onChange={event => setDescription(event.target.value)} placeholder="Что требуется изменить и какой результат ожидается?" />
    <div className="pj-agent-buttons"><button disabled={busy || !description.trim()} onClick={() => request('assistant.draft', { description })}>Подготовить задачу и роль</button><button disabled={busy} onClick={() => request('ai.evals', {})}>Проверочные сценарии</button><button disabled={busy} onClick={() => request('ai.usage', {})}>Журнал и задержки</button></div>
    <div className="pj-agent-selects">
      <label>Задача<select value={taskId} onChange={event => {setTaskId(event.target.value);setRunId('');setResult('');setEvents([]);requestSequence.current++;setBusy(false);}}><option value="">Выберите задачу</option>{tasks.map(task => <option value={task.id} key={task.id}>{task.id} · {task.title}</option>)}</select></label>
      <label>Запуск<select value={runId} onChange={event => {setRunId(event.target.value);setResult('');requestSequence.current++;setBusy(false);}}><option value="">Выберите запуск</option>{runs.filter(run => (!taskId || run.task_id === taskId)&&run.id.startsWith('run_')).map(run => <option key={run.id} value={run.id}>{run.id} · {run.status}</option>)}</select></label>
    </div>
    <div className="pj-agent-buttons"><button disabled={busy || !taskId} onClick={() => request('assistant.handoff', taskArguments)}>Handoff и контекст</button><button disabled={busy || !taskId} onClick={() => request('assistant.review', taskArguments)}>Проверить diff</button><button disabled={busy || !runId} onClick={() => request('run.result', { run_id: runId })}>Результат запуска</button></div>
    {runId && <div className="pj-agent-events" aria-live="polite"><p><RefreshCw size={13} /> События запуска · обновление каждые 3 секунды</p>{events.map(event => <div key={event.cursor}><strong>#{event.cursor} · {event.type}</strong><small>{event.ts}</small></div>)}{!events.length && <p>События ещё не получены.</p>}</div>}
    {streamError && <p className="pj-agent-warning">{streamError}</p>}
    {busy && <p role="status">Читаем источники…</p>}
    {error && <p role="alert" className="pj-agent-warning">{error}</p>}
    {result && <><button className="pj-agent-copy" onClick={() => navigator.clipboard.writeText(result).catch(() => setError('Выделите текст и скопируйте вручную.'))}><Copy size={14} /> Скопировать</button><pre tabIndex={0} className="pj-agent-result" aria-live="polite">{result}</pre></>}
    <p className="pj-agent-note">Продолжение работы доступно через сохранённый контекст. Управление сессией требует подтверждённого протокола адаптера; доступные команды запусков показаны в Контуре.</p>
  </section>;
}
