import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, AlertTriangle, ArrowUpRight, Bot, Briefcase, CheckCircle2, ChevronRight,
  Clock3, Copy, FileText, GitBranch, Layers, Link2, ListTodo, Lock, Pause, Play,
  Radio, RefreshCw, Search, ShieldAlert, ShieldCheck, Square, X
} from 'lucide-react';
import {
  Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis
} from 'recharts';

import './project.css';
import DotMeter from './project/DotMeter';
import { SkeletonCard, SkeletonGroup, SkeletonPanel } from './project/Skeleton';
import Spark from './project/Spark';
import ThresholdBar from './project/ThresholdBar';

import {
  armAction, commitAction, describeApiError, fetchState, fetchTask
} from './project/api';
import {
  ageTone, fmtAgo, fmtDateTime, fmtInt, fmtMoney, fmtNumber, fmtPercent, fmtRelative,
  fmtTaskAge, fmtTime, INTEGRATION_TITLES, severityTone, signTone, splitMoney,
  TASK_STATUS_TITLES, taskStatusTone
} from './project/format';
import { useHotkeys, useNow, useUrlState } from './project/hooks';
import type {
  ActionId, ControlAction, Position, ProjectEvent, ProjectState, RangeKey, TaskDetail
} from './project/types';

const TABS = [
  { id: 'overview', name: 'Обзор', icon: Activity, key: '1' },
  { id: 'tasks', name: 'Задачи', icon: ListTodo, key: '2' },
  { id: 'positions', name: 'Позиции', icon: Briefcase, key: '3' },
  { id: 'events', name: 'События', icon: Clock3, key: '4' },
  { id: 'agents', name: 'Агенты', icon: Bot, key: '5' },
  { id: 'integrations', name: 'Интеграции', icon: Link2, key: '6' },
  { id: 'journal', name: 'Знания и отчёты', icon: FileText, key: '7' },
] as const;

type TabId = (typeof TABS)[number]['id'];

const RANGES: { id: RangeKey; label: string }[] = [
  { id: '24h', label: '24 ч' },
  { id: '48h', label: '48 ч' },
  { id: '7d', label: '7 дней' },
  { id: 'all', label: 'Всё' },
];

function Badge({ status, text }: { status: string; text?: string }) {
  const tone = taskStatusTone(status);
  return (
    <span className={`pj-badge pj-${status}`} data-tone={tone}>
      {text || TASK_STATUS_TITLES[status] || status}
    </span>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="pj-empty">{children}</div>;
}

// ── Кнопка действия с удержанием ────────────────────────────────────
interface HoldButtonProps {
  action: ControlAction;
  onTrigger: (action: ControlAction) => void;
  disabled?: boolean;
}

function HoldButton({ action, onTrigger, disabled }: HoldButtonProps) {
  const [holding, setHolding] = useState(false);
  const [progress, setProgress] = useState(0);
  const startRef = useRef<number>(0);
  const frameRef = useRef<number | null>(null);

  const clearHold = useCallback(() => {
    setHolding(false);
    setProgress(0);
    if (frameRef.current) cancelAnimationFrame(frameRef.current);
  }, []);

  const onPointerDown = useCallback(() => {
    if (disabled || !action.available) return;
    setHolding(true);
    startRef.current = Date.now();

    const check = () => {
      const elapsed = Date.now() - startRef.current;
      const pct = Math.min(100, (elapsed / action.hold_ms) * 100);
      setProgress(pct);
      if (pct >= 100) {
        clearHold();
        onTrigger(action);
      } else {
        frameRef.current = requestAnimationFrame(check);
      }
    };
    frameRef.current = requestAnimationFrame(check);
  }, [action, disabled, onTrigger, clearHold]);

  const levelClass =
    action.level === 'emergency'
      ? 'pj-hold-btn--emergency'
      : action.level === 'critical-reset'
      ? 'pj-hold-btn--reset'
      : 'pj-hold-btn--caution';

  const icon =
    action.id.startsWith('kill.') ? <ShieldAlert size={15} /> :
    action.id.startsWith('orders.') ? <Square size={14} /> :
    action.id === 'engine.pause' ? <Pause size={14} /> :
    action.id === 'engine.resume' ? <Play size={14} /> :
    <Lock size={14} />;

  return (
    <button
      type="button"
      className={`pj-hold-btn ${levelClass}`}
      onPointerDown={onPointerDown}
      onPointerUp={clearHold}
      onPointerLeave={clearHold}
      disabled={disabled || !action.available}
      title={action.reason || action.warning || `Удерживайте ${action.hold_ms / 1000} сек для подтверждения`}
      aria-label={`${action.label}. Удерживайте для выполнения`}
    >
      {holding && <span className="pj-hold-progress" style={{ width: `${progress}%` }} />}
      {icon}
      <span>{action.label}</span>
      {holding && <small>({Math.round(progress)}%)</small>}
    </button>
  );
}

// ── Главный компонент ───────────────────────────────────────────────
export default function ProjectDashboard() {
  const [urlState, setUrlState] = useUrlState({
    section: 'overview',
    q: '',
    status: 'active',
    range: '24h',
  });

  const tab = (TABS.some((t) => t.id === urlState.section) ? urlState.section : 'overview') as TabId;
  const range = (RANGES.some((r) => r.id === urlState.range) ? urlState.range : '24h') as RangeKey;
  const search = urlState.q;
  const statusFilter = urlState.status;

  const now = useNow(1000);
  const [data, setData] = useState<ProjectState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<{ msg: string; err?: boolean } | null>(null);

  // Модалка карточки задачи
  const [selectedTask, setSelectedTask] = useState<TaskDetail | { id: string; loading: true } | null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // Модалка подтверждения сброса (reset phrase)
  const [resetModalAction, setResetModalAction] = useState<{
    action: ControlAction;
    armId: string;
    phrase: string;
    input: string;
    submitting: boolean;
  } | null>(null);

  const searchInputRef = useRef<HTMLInputElement>(null);

  // Загрузка состояния
  const loadState = useCallback(async () => {
    setLoading(true);
    try {
      const state = await fetchState();
      setData(state);
      setError(null);
    } catch (e: any) {
      setError(e.code || e.message || 'unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadState();
    const interval = setInterval(() => { void loadState(); }, 15000);
    return () => clearInterval(interval);
  }, [loadState]);

  // Горячие клавиши
  useHotkeys({
    r: () => void loadState(),
    '1': () => setUrlState({ section: 'overview' }),
    '2': () => setUrlState({ section: 'tasks' }),
    '3': () => setUrlState({ section: 'positions' }),
    '4': () => setUrlState({ section: 'events' }),
    '5': () => setUrlState({ section: 'agents' }),
    '6': () => setUrlState({ section: 'integrations' }),
    '7': () => setUrlState({ section: 'journal' }),
    '/': () => searchInputRef.current?.focus(),
    Escape: () => {
      setSelectedTask(null);
      setResetModalAction(null);
    },
  });

  // Открытие задачи
  const openTask = async (id: string) => {
    setSelectedTask({ id, loading: true });
    setTaskError(null);
    setCopied(false);
    try {
      const task = await fetchTask(id);
      setSelectedTask(task);
    } catch (err) {
      setSelectedTask(null);
      setTaskError('Не удалось загрузить карточку задачи.');
    }
  };

  const copyTask = async () => {
    if (!selectedTask || 'loading' in selectedTask) return;
    try {
      await navigator.clipboard.writeText(
        `Задача ${selectedTask.id}: ${selectedTask.title}\nСтатус: ${selectedTask.status}\nАгент: ${selectedTask.agent}\nЗависимости: ${selectedTask.deps?.join(', ') || '—'}\n\nКритерий готовности:\n${selectedTask.criterion}\n\nЗаметки:\n${selectedTask.notes}`
      );
      setCopied(true);
    } catch {
      setTaskError('Не удалось скопировать в буфер обмена.');
    }
  };

  // Выполнение действия из тулбара
  const handleActionTrigger = async (action: ControlAction) => {
    setActionNotice(null);
    try {
      const armed = await armAction(action.id);
      if (armed.confirm_phrase) {
        setResetModalAction({
          action,
          armId: armed.arm_id,
          phrase: armed.confirm_phrase,
          input: '',
          submitting: false,
        });
      } else {
        // Обычное действие без фразы — коммитим сразу после завершения удержания
        const res = await commitAction(armed.arm_id);
        setActionNotice({ msg: res.summary || 'Действие успешно выполнено' });
        await loadState();
      }
    } catch (err: any) {
      setActionNotice({ msg: describeApiError(err), err: true });
    }
  };

  const submitResetConfirm = async () => {
    if (!resetModalAction) return;
    setResetModalAction((prev) => prev ? { ...prev, submitting: true } : null);
    try {
      const res = await commitAction(resetModalAction.armId, resetModalAction.input.trim());
      setResetModalAction(null);
      setActionNotice({ msg: res.summary || 'Блокировка успешно сброшена' });
      await loadState();
    } catch (err: any) {
      setActionNotice({ msg: describeApiError(err), err: true });
      setResetModalAction(null);
    }
  };

  // Фильтрация задач
  const tasks = useMemo(() => data?.board?.tasks || [], [data?.board?.tasks]);
  const filteredTasks = useMemo(() => {
    return tasks.filter((t) => {
      const matchesSearch =
        !search ||
        `${t.id} ${t.title} ${t.agent}`.toLowerCase().includes(search.toLowerCase());
      const matchesStatus =
        statusFilter === 'all' ||
        (statusFilter === 'active' && t.status !== 'done') ||
        t.status === statusFilter;
      return matchesSearch && matchesStatus;
    });
  }, [tasks, search, statusFilter]);

  const needsUser = useMemo(() => tasks.filter((t) => t.status === 'needs-user'), [tasks]);

  const risk = data?.risk || null;
  const engine = data?.engine || null;
  const lifetime = data?.lifetime || null;
  const positions = data?.positions || null;
  const events = data?.events || null;
  const controls = data?.controls || null;
  const sources = data?.sources || {};

  // Данные для графика капитала с выбранным диапазоном
  const chartData = useMemo(() => {
    const raw = data?.equity?.ranges?.[range] || data?.equity_history || [];
    return raw
      .filter((p: any) => Array.isArray(p) && typeof p[1] === 'number')
      .map((p: any) => ({ ts: p[0], equity: p[1] }));
  }, [data?.equity?.ranges, data?.equity_history, range]);

  const baselineUsdt = data?.equity?.baseline_usdt ?? lifetime?.base_usdt ?? null;
  const hwmUsdt = data?.equity?.hwm ?? risk?.hwm ?? null;
  const blocked = risk?.kill_active || risk?.daily_breaker || risk?.global_breaker;

  return (
    <div className="pj-root">
      {/* ── Шапка ── */}
      <header className="pj-header">
        <div>
          <div className="pj-eyebrow">
            <span className="pj-dot" /> OKX · РАБОЧЕЕ ПРОСТРАНСТВО · DEMO
          </div>
          <h1>Проект под контролем</h1>
          <p>Единая доска задач, агенты и операционное состояние торговли OKX.</p>
        </div>

        <div className="pj-header-actions">
          <span className="pj-demo">DEMO MODE</span>
          <button
            type="button"
            className="pj-icon-button"
            onClick={() => void loadState()}
            aria-label="Обновить состояние (R)"
            title="Обновить состояние (R)"
            disabled={loading}
          >
            <RefreshCw size={17} className={loading ? 'pj-spin' : ''} />
          </button>
        </div>
      </header>

      {/* ── Полоса свежести источников ── */}
      <div className="pj-sources-strip" aria-label="Свежесть источников данных">
        <span className="pj-sources-label">Источники:</span>
        <div className="pj-source-pill">
          <span className={`pj-source-dot ${sources.risk?.ok ? 'ok' : 'crit'}`} />
          <span>Риск</span>
          {sources.risk?.age_s != null && <small>{fmtAgo(sources.risk.age_s)}</small>}
        </div>
        <div className="pj-source-pill">
          <span className={`pj-source-dot ${sources.engine?.ok ? 'ok' : 'crit'}`} />
          <span>Движок</span>
          {sources.engine?.age_s != null && <small>{fmtAgo(sources.engine.age_s)}</small>}
        </div>
        <div className="pj-source-pill">
          <span className={`pj-source-dot ${sources.equity?.ok ? 'ok' : 'crit'}`} />
          <span>Капитал</span>
          {sources.equity?.points ? <small>({sources.equity.points} точек)</small> : null}
        </div>
        <div className="pj-source-pill">
          <span className={`pj-source-dot ${sources.positions?.ok ? 'ok' : 'warn'}`} />
          <span>Позиции</span>
          {positions?.count ? <small>({positions.count})</small> : null}
        </div>
        <div className="pj-source-pill">
          <span className={`pj-source-dot ${sources.events?.ok ? 'ok' : 'warn'}`} />
          <span>События</span>
          {events?.items?.length ? <small>({events.items.length})</small> : null}
        </div>
        <div className="pj-source-pill">
          <span className={`pj-source-dot ${sources.guard?.ok ? 'ok' : 'warn'}`} />
          <span>Guard</span>
        </div>
      </div>

      {/* ── Тулбар управления (Controls) ── */}
      {controls?.enabled && controls.actions.length > 0 && (
        <section className="pj-controls-bar" aria-label="Панель безопасности и действий">
          <div className="pj-controls-info">
            <Radio size={16} color="var(--pj-accent)" />
            <span><strong>Управление demo:</strong> удерживайте кнопку для подтверждения</span>
          </div>
          <div className="pj-controls-actions">
            {controls.actions.map((act) => (
              <HoldButton
                key={act.id}
                action={act}
                onTrigger={handleActionTrigger}
                disabled={loading}
              />
            ))}
          </div>
        </section>
      )}

      {/* ── Уведомления и ошибки ── */}
      {actionNotice && (
        <div role="status" className={`pj-alert ${actionNotice.err ? 'error' : ''}`}>
          {actionNotice.msg}
        </div>
      )}

      {error === 'login' ? (
        <div className="pj-notice">
          <ShieldCheck size={20} />
          <div>
            <strong>Требуется авторизация в Morphy</strong>
            <p>Панель проекта использует сессию Morphy. Войдите в систему для доступа.</p>
            <a href="/bloby" target="_top">
              Вход в Morphy <ArrowUpRight size={14} />
            </a>
          </div>
        </div>
      ) : error ? (
        <div role="alert" className="pj-alert error">
          Не удалось обновить снимок проекта ({error}). Данные могут быть устаревшими.
        </div>
      ) : null}

      {taskError && (
        <div role="alert" className="pj-alert error">
          {taskError}
        </div>
      )}

      {/* ── Вкладки ── */}
      <nav className="pj-tabs" aria-label="Разделы проекта">
        {TABS.map(({ id, name, icon: Icon, key }) => (
          <button
            key={id}
            type="button"
            aria-current={tab === id ? 'page' : undefined}
            className={tab === id ? 'selected' : ''}
            onClick={() => setUrlState({ section: id })}
          >
            <Icon size={16} />
            <span>{name}</span>
            {id === 'tasks' && needsUser.length > 0 && (
              <span className="pj-tab-count">{needsUser.length}</span>
            )}
            {id === 'positions' && (positions?.count || 0) > 0 && (
              <span className="pj-tab-count" style={{ background: 'var(--pj-accent-bg)', color: 'var(--pj-accent)' }}>
                {positions?.count}
              </span>
            )}
          </button>
        ))}
      </nav>

      {/* ── Индикатор первичной загрузки ── */}
      {!data && !error && (
        <SkeletonGroup label="Загрузка данных проекта...">
          <div className="pj-metrics">
            <SkeletonCard spark={true} />
            <SkeletonCard spark={true} />
            <SkeletonCard spark={true} />
            <SkeletonCard spark={true} />
            <SkeletonCard spark={false} />
          </div>
          <SkeletonPanel chart={true} />
        </SkeletonGroup>
      )}

      {/* ── Контент вкладки «Обзор» (Overview) ── */}
      {data && tab === 'overview' && (
        <>
          <div className="pj-metrics">
            {/* 1. Капитал demo */}
            <div className="pj-metric">
              <span>
                Капитал demo
                <small>{sources.risk?.age_s != null ? fmtAgo(sources.risk.age_s) : ''}</small>
              </span>
              <strong>{splitMoney(risk?.equity).text}</strong>
              <Spark
                data={data.spark?.equity}
                label="Капитал 24ч"
                tone="auto"
                height={36}
                empty="stub"
              />
              <small>
                {baselineUsdt ? `База: ${fmtNumber(baselineUsdt, 0)}` : 'База неизвестна'}
                {hwmUsdt ? ` · Пик: ${fmtNumber(hwmUsdt, 0)} USDT` : ''}
              </small>
            </div>

            {/* 2. Результат дня */}
            <div className="pj-metric" data-tone={signTone(risk?.day_pnl)}>
              <span>
                Результат дня
                <small>{fmtPercent(risk?.day_pnl_pct, { sign: true })}</small>
              </span>
              <strong>{splitMoney(risk?.day_pnl, { sign: true }).text}</strong>
              <Spark
                data={data.spark?.day_pnl}
                label="PnL за день"
                baseline={0}
                tone="auto"
                height={36}
                empty="stub"
              />
              <small>за текущие риск-сутки</small>
            </div>

            {/* 3. Просадка */}
            <div className="pj-metric" data-tone={(risk?.drawdown_pct || 0) < -10 ? 'warn' : 'neutral'}>
              <span>
                Просадка от пика
                <small>Лимит {fmtPercent(risk?.global_dd_limit_pct)}</small>
              </span>
              <strong>
                {risk?.drawdown_pct != null
                  ? fmtPercent(Math.abs(risk.drawdown_pct))
                  : '—'}
              </strong>
              <Spark
                data={data.spark?.drawdown_pct}
                label="Просадка 7д"
                tone="warn"
                height={36}
                empty="stub"
              />
              <ThresholdBar
                value={risk?.drawdown_pct ? Math.abs(risk.drawdown_pct) : 0}
                limit={risk?.global_dd_limit_pct || 15}
                label="Просадка"
                unit="%"
                hideLabel={true}
              />
            </div>

            {/* 4. Прибыль за всё время */}
            <div className="pj-metric" data-tone={signTone(lifetime?.net_usdt)}>
              <span>
                Чистая прибыль
                <small>{lifetime?.since ? `с ${fmtTime(lifetime.since)}` : ''}</small>
              </span>
              <strong>{splitMoney(lifetime?.net_usdt, { sign: true }).text}</strong>
              <Spark
                data={data.spark?.lifetime_net}
                label="Накопленный PnL"
                baseline={0}
                tone="auto"
                height={36}
                empty="stub"
              />
              <small>
                {lifetime?.net_pct != null ? `${fmtPercent(lifetime.net_pct, { sign: true })} · ` : ''}
                пополнения demo исключены
              </small>
            </div>

            {/* 5. Движок */}
            <div className="pj-metric" data-tone={engine?.running ? 'ok' : 'warn'}>
              <span>
                Движок OKX
                <small>{engine?.running ? 'Онлайн' : 'Остановлен'}</small>
              </span>
              <strong>{engine?.running ? 'В работе' : 'Остановлен'}</strong>
              <small style={{ marginTop: 'auto' }}>
                {engine?.uptime_h != null ? `${fmtNumber(engine.uptime_h, 1)} ч аптайм` : '—'}
                {engine?.reconciles != null ? ` · ${fmtInt(engine.reconciles)} сверок` : ''}
              </small>
            </div>
          </div>

          <div className="pj-overview-grid">
            {/* График капитала с переключателем диапазонов */}
            <section className="pj-panel">
              <div className="pj-section-head">
                <div>
                  <h2>Кривая капитала</h2>
                  <p>
                    {range === '24h' ? 'Последние 24 часа' :
                     range === '48h' ? 'Последние 48 часов' :
                     range === '7d' ? 'За 7 суток' : 'Вся история'}
                  </p>
                </div>
                <div className="pj-ranges" role="group" aria-label="Период графика">
                  {RANGES.map((r) => (
                    <button
                      key={r.id}
                      type="button"
                      className={`pj-range-btn ${range === r.id ? 'selected' : ''}`}
                      onClick={() => setUrlState({ range: r.id })}
                    >
                      {r.label}
                    </button>
                  ))}
                </div>
              </div>

              {chartData.length > 1 ? (
                <div className="pj-chart">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={chartData} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
                      <defs>
                        <linearGradient id="pj-equity-grad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="var(--pj-accent)" stopOpacity={0.25} />
                          <stop offset="100%" stopColor="var(--pj-accent)" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke="var(--pj-chart-grid)" vertical={false} />
                      <XAxis
                        dataKey="ts"
                        tickFormatter={(v) => fmtTime(v)}
                        tick={{ fill: 'var(--pj-chart-axis)', fontSize: 10 }}
                        axisLine={false}
                        tickLine={false}
                        minTickGap={45}
                      />
                      <YAxis
                        domain={['auto', 'auto']}
                        tickFormatter={(v) => fmtNumber(v, 0)}
                        tick={{ fill: 'var(--pj-chart-axis)', fontSize: 10 }}
                        width={60}
                        axisLine={false}
                        tickLine={false}
                      />
                      <Tooltip
                        contentStyle={{
                          background: 'var(--pj-surface-2)',
                          borderColor: 'var(--pj-line-strong)',
                          borderRadius: 9,
                          fontSize: 12,
                        }}
                        labelFormatter={(v) => fmtDateTime(v)}
                        formatter={(val: any) => [`${fmtMoney(val)}`, 'Капитал']}
                      />
                      {baselineUsdt && (
                        <ReferenceLine
                          y={baselineUsdt}
                          stroke="var(--pj-chart-baseline)"
                          strokeDasharray="4 4"
                        />
                      )}
                      {hwmUsdt && (
                        <ReferenceLine
                          y={hwmUsdt}
                          stroke="var(--pj-chart-hwm)"
                          strokeDasharray="3 3"
                        />
                      )}
                      <Area
                        type="monotone"
                        dataKey="equity"
                        stroke="var(--pj-accent)"
                        strokeWidth={2}
                        fill="url(#pj-equity-grad)"
                        isAnimationActive={false}
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <Empty>Недостаточно точек для графика за этот период</Empty>
              )}
            </section>

            {/* Безопасность и лимиты */}
            <section className="pj-panel">
              <div className="pj-section-head">
                <div>
                  <h2>Безопасность и риск</h2>
                  <p>Состояние риск-ядра и лимиты сессии</p>
                </div>
                <ShieldCheck size={18} />
              </div>

              <div className={`pj-safety-banner ${blocked ? 'blocked' : 'ok'}`}>
                {blocked ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}
                <div>
                  <strong>{blocked ? 'Входы заблокированы' : 'Блокировки не активны'}</strong>
                  <small>
                    {risk?.kill_active ? 'Kill-switch активен' :
                     risk?.global_breaker ? 'Сработал глобальный breaker' :
                     risk?.daily_breaker ? 'Сработал дневной breaker' :
                     'Торговля и выставление ордеров разрешены'}
                  </small>
                </div>
              </div>

              <ThresholdBar
                value={risk?.portfolio_heat_pct}
                limit={risk?.max_heat_pct}
                label="Риск открытых позиций"
                unit="%"
                className="pj-risk-row"
              />

              <div style={{ marginTop: 14 }}>
                <DotMeter
                  value={risk?.entries_today}
                  max={risk?.max_entries_per_day}
                  label="Входов за день"
                />
              </div>

              <div className="pj-risk-row" style={{ marginTop: 16 }}>
                <span>Ошибки движка (internal / exchange)</span>
                <strong>
                  {fmtInt(engine?.errors_internal)} / {fmtInt(engine?.errors_exchange)}
                </strong>
              </div>

              <div className="pj-risk-row">
                <span>Отказы guard за 24 ч.</span>
                <strong>{fmtInt(data.ops?.guard?.denies_24h)}</strong>
              </div>
            </section>
          </div>

          {/* Задачи, ожидающие пользователя + Работа агентов */}
          <div className="pj-overview-grid">
            <section className="pj-panel">
              <div className="pj-section-head">
                <div>
                  <h2>Нужно решение человека ({needsUser.length})</h2>
                  <p>Вопросы из единой доски проекта</p>
                </div>
                <button
                  type="button"
                  className="pj-icon-button"
                  style={{ width: 'auto', padding: '0 10px', height: 28, fontSize: 11 }}
                  onClick={() => {
                    setUrlState({ section: 'tasks', status: 'needs-user' });
                  }}
                >
                  Все вопросы <ArrowUpRight size={13} />
                </button>
              </div>

              {needsUser.length > 0 ? (
                <div className="pj-task-list">
                  {needsUser.slice(0, 4).map((t) => (
                    <button
                      key={t.id + ':' + t.line}
                      type="button"
                      className="pj-task"
                      onClick={() => openTask(t.id)}
                    >
                      <div className="pj-task-main">
                        <span className="pj-id">{t.id}</span>
                        <strong>{t.title}</strong>
                        <small>{t.agent}</small>
                      </div>
                      <div className="pj-task-status">
                        <Badge status={t.status} />
                        {t.age_days != null && (
                          <small data-tone={ageTone(t.age_days)}>
                            {fmtTaskAge(t.age_days)}
                          </small>
                        )}
                      </div>
                      <ChevronRight size={16} />
                    </button>
                  ))}
                </div>
              ) : (
                <Empty>Нет открытых вопросов, требующих решения</Empty>
              )}
            </section>

            <section className="pj-panel">
              <div className="pj-section-head">
                <div>
                  <h2>Текущая работа команды ({data.claims?.length || 0})</h2>
                  <p>Задачи в статусе in-progress</p>
                </div>
                <Bot size={18} />
              </div>

              {data.claims?.length ? (
                <div className="pj-task-list">
                  {data.claims.slice(0, 4).map((t) => (
                    <button
                      key={t.id + ':' + t.line}
                      type="button"
                      className="pj-task"
                      onClick={() => openTask(t.id)}
                    >
                      <div className="pj-task-main">
                        <span className="pj-id">{t.id}</span>
                        <strong>{t.title}</strong>
                        <small>{t.agent}</small>
                      </div>
                      <div className="pj-task-status">
                        <Badge status={t.status} text={t.status_detail || t.status} />
                        {t.age_days != null && (
                          <small data-tone={ageTone(t.age_days)}>
                            {fmtTaskAge(t.age_days)}
                          </small>
                        )}
                      </div>
                      <ChevronRight size={16} />
                    </button>
                  ))}
                </div>
              ) : (
                <Empty>Сейчас активных клеймов нет</Empty>
              )}
            </section>
          </div>
        </>
      )}

      {/* ── Вкладка «Позиции» (Positions) ── */}
      {data && tab === 'positions' && (
        <section className="pj-panel">
          <div className="pj-section-head">
            <div>
              <h2>Открытые позиции ({positions?.count || 0})</h2>
              <p>{positions?.note || 'Локальный снимок открытых позиций движка и кармана'}</p>
            </div>
            {positions?.total_upl != null && (
              <span className="pj-demo" style={{ color: positions.total_upl >= 0 ? 'var(--pj-money-pos)' : 'var(--pj-money-neg)' }}>
                uPnL: {splitMoney(positions.total_upl, { sign: true }).text}
              </span>
            )}
          </div>

          {positions?.items && positions.items.length > 0 ? (
            <div className="pj-table-container">
              <table className="pj-table">
                <thead>
                  <tr>
                    <th>Инструмент</th>
                    <th>Сторона</th>
                    <th>Размер</th>
                    <th>Вход</th>
                    <th>uPnL</th>
                    <th>Стоп-лосс</th>
                    <th>Возраст</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.items.map((p: Position) => (
                    <tr key={p.id}>
                      <td>
                        <strong>{p.symbol}</strong>
                        <small style={{ display: 'block', color: 'var(--pj-text-faint)' }}>{p.source}</small>
                      </td>
                      <td>
                        <span className={`pj-side-badge ${p.side}`}>
                          {p.side}
                        </span>
                      </td>
                      <td>
                        <span className="pj-num">{fmtNumber(p.size, 4)}</span>
                        {p.notional != null && (
                          <small style={{ display: 'block', color: 'var(--pj-text-muted)' }}>
                            {fmtMoney(p.notional)}
                          </small>
                        )}
                      </td>
                      <td className="pj-num">{fmtNumber(p.entry, 2)}</td>
                      <td style={{ color: (p.upl || 0) >= 0 ? 'var(--pj-money-pos)' : 'var(--pj-money-neg)' }}>
                        <span className="pj-num">{p.upl != null ? fmtMoney(p.upl, { sign: true }) : '—'}</span>
                        {p.upl_pct != null && (
                          <small style={{ display: 'block' }}>{fmtPercent(p.upl_pct, { sign: true })}</small>
                        )}
                      </td>
                      <td>
                        <span className="pj-num">{p.stop != null ? fmtNumber(p.stop, 2) : '—'}</span>
                        {p.stop_distance_pct != null && (
                          <small style={{ display: 'block', color: 'var(--pj-text-muted)' }}>
                            {fmtPercent(p.stop_distance_pct)} от входа
                          </small>
                        )}
                      </td>
                      <td>{p.age_s != null ? fmtAgo(p.age_s) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty>Открытых позиций сейчас нет</Empty>
          )}
        </section>
      )}

      {/* ── Вкладка «События» (Events) ── */}
      {data && tab === 'events' && (
        <section className="pj-panel">
          <div className="pj-section-head">
            <div>
              <h2>Лента событий проекта</h2>
              <p>Последние 100 событий ордеров, риска, guard и движка</p>
            </div>
            <span className="pj-id">
              Источники: {events?.sources?.join(', ') || 'нет'}
            </span>
          </div>

          {events?.items && events.items.length > 0 ? (
            <div className="pj-feed">
              {events.items.map((ev: ProjectEvent, i: number) => {
                const sTone = severityTone(ev.severity);
                return (
                  <div className="pj-feed-item" key={ev.ts + ':' + i}>
                    <span className="pj-feed-time">{fmtRelative(ev.ts, now)}</span>
                    <span
                      className="pj-feed-badge"
                      style={{
                        background:
                          sTone === 'crit' ? 'var(--pj-crit-bg)' :
                          sTone === 'warn' ? 'var(--pj-warn-bg)' : 'var(--pj-surface-3)',
                        color:
                          sTone === 'crit' ? 'var(--pj-crit)' :
                          sTone === 'warn' ? 'var(--pj-warn)' : 'var(--pj-text-2)',
                      }}
                    >
                      {ev.kind}
                    </span>
                    <div className="pj-feed-content">
                      <div className="pj-feed-title">{ev.title}</div>
                      {ev.detail && <div className="pj-feed-detail">{ev.detail}</div>}
                      <div className="pj-feed-source">Источник: {ev.source} · {fmtTime(ev.ts)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <Empty>События пока отсутствуют</Empty>
          )}
        </section>
      )}

      {/* ── Вкладка «Задачи» (Tasks) ── */}
      {data && tab === 'tasks' && (
        <section className="pj-panel">
          <div className="pj-section-head">
            <div>
              <h2>Единая доска задач</h2>
              <p>
                Всего {tasks.length} задач · {data.board?.ready_ids?.length || 0} готовы к взятию в работу
              </p>
            </div>
          </div>

          <div className="pj-filters">
            <label className="pj-search">
              <Search size={16} />
              <input
                ref={searchInputRef}
                aria-label="Поиск задач (/)"
                placeholder="Поиск по ID, названию или агенту (/)"
                value={search}
                onChange={(e) => setUrlState({ q: e.target.value }, { replace: true })}
              />
            </label>

            <select
              aria-label="Фильтр статуса"
              value={statusFilter}
              onChange={(e) => setUrlState({ status: e.target.value })}
            >
              <option value="active">Все активные</option>
              <option value="all">Все (включая выполненные)</option>
              {Object.entries(TASK_STATUS_TITLES).map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </div>

          {data.board?.duplicates && data.board.duplicates.length > 0 && (
            <div className="pj-alert">
              Внимание: найдены дубликаты ID на доске ({data.board.duplicates.join(', ')}).
            </div>
          )}

          <div className="pj-task-list">
            {filteredTasks.length > 0 ? (
              filteredTasks.map((t) => (
                <button
                  key={t.id + ':' + t.line}
                  type="button"
                  className="pj-task"
                  onClick={() => openTask(t.id)}
                >
                  <div className="pj-task-main">
                    <span className="pj-id">{t.id}</span>
                    <strong>{t.title}</strong>
                    <small>{t.agent}</small>
                  </div>
                  <div className="pj-task-status">
                    <Badge status={t.status} text={t.status_detail || t.status} />
                    {t.age_days != null && (
                      <small data-tone={ageTone(t.age_days)}>
                        {fmtTaskAge(t.age_days)}
                      </small>
                    )}
                  </div>
                  <ChevronRight size={16} />
                </button>
              ))
            ) : (
              <Empty>Задач по выбранному фильтру не найдено</Empty>
            )}
          </div>
        </section>
      )}

      {/* ── Вкладка «Агенты» (Agents) ── */}
      {data && tab === 'agents' && (
        <>
          <div className="pj-runtime-grid">
            {data.runtimes?.map((r) => (
              <section className="pj-panel pj-runtime" key={r.id}>
                <div className="pj-runtime-icon">
                  <Bot size={24} />
                </div>
                <h2>
                  {r.id === 'claude' ? 'Claude Code' :
                   r.id === 'gemini' ? 'Gemini CLI' :
                   r.id === 'codex' ? 'Codex' :
                   r.id === 'muse' ? 'Meta Muse' : r.id}
                </h2>
                <p>{r.model || 'Модель из реестра не указана'}</p>
                <Badge
                  status={r.cli_available ? 'done' : 'blocked'}
                  text={r.cli_available ? 'CLI доступен' : 'CLI не найден'}
                />
                <small>Guard: {r.guard_status}</small>
              </section>
            ))}
          </div>

          <section className="pj-panel">
            <div className="pj-section-head">
              <div>
                <h2>Активные клеймы агентов ({data.claims?.length || 0})</h2>
                <p>Текущие задачи в работе</p>
              </div>
            </div>
            <div className="pj-task-list">
              {data.claims?.length ? (
                data.claims.map((t) => (
                  <button
                    key={t.id + ':' + t.line}
                    type="button"
                    className="pj-task"
                    onClick={() => openTask(t.id)}
                  >
                    <div className="pj-task-main">
                      <span className="pj-id">{t.id}</span>
                      <strong>{t.title}</strong>
                      <small>{t.agent}</small>
                    </div>
                    <div className="pj-task-status">
                      <Badge status={t.status} text={t.status_detail || t.status} />
                      {t.age_days != null && (
                        <small data-tone={ageTone(t.age_days)}>
                          {fmtTaskAge(t.age_days)}
                        </small>
                      )}
                    </div>
                    <ChevronRight size={16} />
                  </button>
                ))
              ) : (
                <Empty>Нет задач в работе</Empty>
              )}
            </div>
          </section>

          <section className="pj-panel">
            <div className="pj-section-head">
              <div>
                <h2>Очередь Muse</h2>
                <p>{data.queue?.paused ? 'Очередь на паузе' : 'Файловая очередь активна'}</p>
              </div>
            </div>
            {data.queue?.jobs?.length ? (
              <div className="pj-feed">
                {data.queue.jobs.slice(0, 8).map((job) => (
                  <div className="pj-feed-item" key={job.id}>
                    <span className="pj-id" style={{ minWidth: 90 }}>{job.id}</span>
                    <span className="pj-feed-badge">{job.role || 'Делегирование'}</span>
                    <div className="pj-feed-content">
                      <Badge status={job.state} text={job.state} />
                      <small style={{ marginLeft: 8 }}>{job.provenance}</small>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <Empty>Заявок в очереди делегирования нет</Empty>
            )}
          </section>
        </>
      )}

      {/* ── Вкладка «Интеграции» (Integrations) ── */}
      {data && tab === 'integrations' && (
        <>
          <div className="pj-section-head">
            <div>
              <h2>Интеграции и службы</h2>
              <p>Статус ключевых подсистем проекта</p>
            </div>
          </div>

          <div className="pj-integration-grid">
            {data.integrations?.map((item) => (
              <section className="pj-panel pj-integration" key={item.id}>
                <div className="pj-integration-top">
                  <span className="pj-integration-icon"><Link2 size={18} /></span>
                  <small>{item.group}</small>
                </div>
                <h3>{item.name}</h3>
                <p>{item.detail}</p>
                <Badge
                  status={item.status}
                  text={INTEGRATION_TITLES[item.status] || item.status}
                />
                <small className="pj-source" style={{ marginTop: 10, color: 'var(--pj-text-faint)' }}>
                  Источник: {item.source}
                </small>
                {item.url && (
                  <a href={item.url} target="_blank" rel="noreferrer">
                    Открыть сервис <ArrowUpRight size={13} />
                  </a>
                )}
              </section>
            ))}
          </div>

          <section className="pj-panel">
            <div className="pj-section-head">
              <div>
                <h2>Модули и инфраструктура кода</h2>
                <p>Наличие файлов ядра</p>
              </div>
              <Layers size={18} />
            </div>
            <div className="pj-module-grid">
              {data.modules?.map((m) => (
                <div className="pj-module" key={m.id}>
                  <strong>{m.name}</strong>
                  <p>{m.description}</p>
                  <Badge
                    status={m.present ? 'done' : 'blocked'}
                    text={m.present ? 'Файлы присутствуют' : 'Файлы отсутствуют'}
                  />
                  <small>{m.files.join(' · ')}</small>
                </div>
              ))}
            </div>
          </section>

          <section className="pj-panel">
            <div className="pj-section-head">
              <div>
                <h2>Git репозиторий</h2>
                <p>Состояние рабочей копии</p>
              </div>
              <GitBranch size={18} />
            </div>
            <div className="pj-git">
              <span>Ветка: <strong>{data.git?.branch || '—'}</strong></span>
              <span>Коммит: <strong>{data.git?.commit || '—'}</strong></span>
              <span>Изменено: <strong>{data.git?.changed_files ?? 0} файлов</strong></span>
              <span>GitHub CLI: <strong>{data.git?.github_cli_available ? 'Доступен' : 'Не найден'}</strong></span>
            </div>
          </section>
        </>
      )}

      {/* ── Вкладка «Знания и отчёты» (Journal) ── */}
      {data && tab === 'journal' && (
        <div className="pj-overview-grid">
          <section className="pj-panel">
            <div className="pj-section-head">
              <div>
                <h2>Исследования и отчёты</h2>
                <p>База знаний проекта из каталога insights/</p>
              </div>
              <FileText size={18} />
            </div>
            {data.insights?.length ? (
              data.insights.map((doc) => (
                <div className="pj-document" key={doc.path}>
                  <FileText size={16} />
                  <div>
                    <strong>{doc.title}</strong>
                    <p>{doc.status || 'Без статуса'}</p>
                    <small>{doc.path} · {fmtTime(doc.updated_at)}</small>
                  </div>
                </div>
              ))
            ) : (
              <Empty>Документы отсутствуют</Empty>
            )}
          </section>

          <section className="pj-panel">
            <div className="pj-section-head">
              <div>
                <h2>Журнал инцидентов</h2>
                <p>Записи аварий и сбоев</p>
              </div>
            </div>
            {data.incidents?.length ? (
              data.incidents.map((inc, i) => (
                <div className="pj-document" key={i}>
                  <AlertTriangle size={16} color="var(--pj-warn)" />
                  <div>
                    <strong>{inc.title}</strong>
                    <small>{inc.source}</small>
                  </div>
                </div>
              ))
            ) : (
              <Empty>Инцидентов не зафиксировано</Empty>
            )}

            <div className="pj-section-head" style={{ marginTop: 24 }}>
              <div>
                <h2>Решения из Obsidian ({data.pending_decisions?.length || 0})</h2>
                <p>Новые файлы решений человека</p>
              </div>
            </div>
            {data.pending_decisions?.length ? (
              data.pending_decisions.map((path) => (
                <div className="pj-document" key={path}>
                  <FileText size={15} />
                  <small>{path}</small>
                </div>
              ))
            ) : (
              <Empty>Все решения перенесены на доску</Empty>
            )}
          </section>
        </div>
      )}

      {/* ── Подвал ── */}
      {data && (
        <footer className="pj-footer">
          <span>
            <span className="pj-dot" /> Обновлено {fmtTime(data.generated_at)} ({fmtAgo((now - Date.parse(data.generated_at)) / 1000)}) · автоопрос каждые 15 сек.
          </span>
          <span>
            Снимок локального состояния
            {data.errors?.length ? ` · Ошибки источников: ${data.errors.join(', ')}` : ''}
          </span>
        </footer>
      )}

      {/* ── Модалка детальной карточки задачи ── */}
      {selectedTask && (
        <div className="pj-modal-backdrop" onClick={() => setSelectedTask(null)}>
          <section
            className="pj-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="pj-task-title"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              className="pj-modal-close pj-icon-button"
              aria-label="Закрыть"
              onClick={() => setSelectedTask(null)}
            >
              <X size={18} />
            </button>

            <span className="pj-id">{selectedTask.id}</span>
            <h2 id="pj-task-title">
              {'loading' in selectedTask ? 'Загрузка карточки...' : selectedTask.title}
            </h2>

            {!('loading' in selectedTask) && (
              <>
                <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 14 }}>
                  <Badge status={selectedTask.status} text={selectedTask.status_detail || selectedTask.status} />
                  <span style={{ fontSize: 12, color: 'var(--pj-text-muted)' }}>{selectedTask.agent}</span>
                </div>

                <h3>Критерий готовности</h3>
                <p style={{ whiteSpace: 'pre-wrap' }}>{selectedTask.criterion || 'Не указан'}</p>

                <h3>Зависимости</h3>
                <p>{selectedTask.deps?.length ? selectedTask.deps.join(', ') : 'Нет зависимостей'}</p>

                <h3>Заметки и решения</h3>
                <p style={{ whiteSpace: 'pre-wrap' }}>{selectedTask.notes || 'Заметок нет'}</p>

                <div className="pj-modal-actions">
                  <button type="button" className="pj-primary-button" onClick={copyTask}>
                    <Copy size={14} style={{ marginRight: 6, verticalAlign: 'middle' }} />
                    {copied ? 'Скопировано!' : 'Скопировать карточку'}
                  </button>
                  <small style={{ color: 'var(--pj-text-faint)' }}>
                    Строка {selectedTask.line} в ops/board.md
                  </small>
                </div>
              </>
            )}
          </section>
        </div>
      )}

      {/* ── Модалка подтверждения сброса блокировки ── */}
      {resetModalAction && (
        <div className="pj-modal-backdrop" onClick={() => setResetModalAction(null)}>
          <section
            className="pj-modal"
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              className="pj-modal-close pj-icon-button"
              aria-label="Отмена"
              onClick={() => setResetModalAction(null)}
            >
              <X size={18} />
            </button>

            <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--pj-warn)' }}>
              <AlertTriangle size={24} />
              <h2>{resetModalAction.action.label}</h2>
            </div>

            <p style={{ marginTop: 8 }}>
              {resetModalAction.action.warning || 'Сброс аварийных защит требует явного подтверждения оператора.'}
            </p>

            <p style={{ marginTop: 12, color: 'var(--pj-text-muted)', fontSize: 12 }}>
              Для подтверждения введите точную фразу: <strong>{resetModalAction.phrase}</strong>
            </p>

            <input
              type="text"
              className="pj-input"
              autoFocus
              placeholder={resetModalAction.phrase}
              value={resetModalAction.input}
              onChange={(e) => setResetModalAction({ ...resetModalAction, input: e.target.value })}
            />

            <div className="pj-modal-actions">
              <button
                type="button"
                className="pj-primary-button"
                style={{ background: 'var(--pj-crit-solid)', color: '#fff' }}
                disabled={
                  resetModalAction.submitting ||
                  resetModalAction.input.trim() !== resetModalAction.phrase.trim()
                }
                onClick={submitResetConfirm}
              >
                {resetModalAction.submitting ? 'Выполнение...' : 'Подтвердить сброс'}
              </button>
              <button
                type="button"
                className="pj-icon-button"
                style={{ width: 'auto', padding: '0 14px' }}
                onClick={() => setResetModalAction(null)}
              >
                Отмена
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
