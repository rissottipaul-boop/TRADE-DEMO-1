// Типы состояния экрана проекта и API — зеркало contract.md (пакет MORPHY-UI-*).
// Старые поля (их читает текущий экран) описаны как есть; все поля из §1–2 контракта
// необязательны: старый бэкенд их не отдаёт, и экран обязан деградировать, а не падать.
// Значения, которые Python отдаёт как null при сбое источника, типизированы `| null`.

/** Смысловой тон (data-tone в tokens.css); объявлен рядом с функциями тонов. */
export type { Tone } from './format';

/** ISO-8601 (UTC или со смещением). */
export type Iso = string;
/** Unix-время в секундах. */
export type UnixSec = number;
/** Серия [[unix_sec, значение], …] по возрастанию времени. */
export type Point = [UnixSec, number];
export type Series = Point[];

// ── Доска ─────────────────────────────────────────────────────────────

export type TaskStatus = 'ready' | 'in-progress' | 'blocked' | 'needs-user' | 'scheduled' | 'done';
export type AgeBasis = 'git-history' | 'notes-date';

export interface BoardTask {
  id: string;
  title: string;
  agent: string;
  /** Первое слово ячейки статуса; неизвестные значения возможны. */
  status: TaskStatus | (string & {});
  /** Остаток ячейки статуса: «Codex 07:20», «2026-10-04T10:30+05:00». */
  status_detail: string;
  deps: string[];
  /** Номер строки в ops/board.md. */
  line: number;
  duplicate?: boolean;
  /** Новое (§1): когда задача вошла в текущий статус; неизвестно → null. */
  since?: Iso | null;
  age_days?: number | null;
  age_basis?: AgeBasis | null;
}

export interface Board {
  counts: Record<string, number>;
  tasks: BoardTask[];
  ready_ids: string[];
  duplicates: string[];
}

/** GET /api/project/task/:id */
export interface TaskDetail {
  id: string;
  title: string;
  agent: string;
  status: BoardTask['status'];
  status_detail: string;
  deps: string[];
  eligible: boolean;
  criterion: string;
  notes: string;
  line: number;
}

// ── Агенты и очередь ─────────────────────────────────────────────────

export interface Runtime {
  id: 'claude' | 'codex' | 'gemini' | 'muse' | (string & {});
  model: string | null;
  guard_status: string;
  cli_available: boolean;
}

export type QueueJobState = 'done' | 'error' | 'timeout' | 'processing-unverified' | 'queued' | 'unknown';

export interface QueueJob {
  id: string;
  runtime: string;
  state: QueueJobState | (string & {});
  role: string | null;
  from: string | null;
  created: string | null;
  finished: string | null;
  exit_code: number | null;
  elapsed_s: number | null;
  provenance: string;
  cost_usd: number | null;
  cost_quality: string;
}

export interface QueueResult {
  id: string;
  status: string | null;
  exit_code: number | null;
  finished: string | null;
  elapsed_s: number | null;
}

export interface Queue {
  counts: Record<string, number>;
  paused: boolean | null;
  runner_pid_recorded?: boolean;
  results?: QueueResult[];
  jobs: QueueJob[];
}

// ── Торговля: движок, риск, карман ───────────────────────────────────

export interface EngineState {
  running: boolean | null;
  uptime_h: number | null;
  reconciles: number | null;
  divergences: number | null;
  errors_exchange: number | null;
  errors_internal: number | null;
  stats_age_s: number | null;
  ws_public_reconnects: number | null;
  ws_private_reconnects: number | null;
}

export interface RiskState {
  equity: number | null;
  hwm: number | null;
  /** Отрицательная при просадке: (equity / hwm − 1) × 100. */
  drawdown_pct: number | null;
  day_pnl: number | null;
  day_pnl_pct: number | null;
  daily_limit_pct: number | null;
  global_dd_limit_pct: number | null;
  daily_breaker: boolean | null;
  global_breaker: boolean | null;
  kill_active: boolean | null;
  equity_age_s: number | null;
  entries_today: number | null;
  max_entries_per_day: number | null;
  portfolio_heat_pct: number | null;
  max_heat_pct: number | null;
  /** Новое (§1): капитал на начало риск-суток. */
  day_start_equity?: number | null;
}

export interface Flags {
  KILL: boolean | null;
  STOP_ENGINE: boolean | null;
}

export interface PumpState {
  entry_allowed: boolean | null;
  budget_total: number | null;
  budget_free: number | null;
  in_positions: number | null;
  day_pnl: number | null;
  day_limit: number | null;
  drawdown: number | null;
  drawdown_limit: number | null;
  open_positions: unknown[] | null;
}

export interface Lifetime {
  available: boolean;
  points?: number;
  net_usdt?: number;
  net_pct?: number | null;
  base_usdt?: number;
  external_usdt?: number;
  external_flows?: number;
  since?: Iso;
  equity_usdt?: number;
}

export interface GuardStats {
  denies_24h: number | null;
  errors_24h: number | null;
  last_deny_at: Iso | null;
}

export interface LiveState {
  enabled: boolean | null;
  until: Iso | null;
  open: boolean | null;
  hours_left: number | null;
}

export interface OpsState {
  live: LiveState | null;
  guard: GuardStats | null;
  engine_log_age_s: number | null;
  autostart_off: boolean | null;
}

export interface Netdata {
  available: boolean | null;
  version: string | null;
  cpu_cores: string | number | null;
  alarms_critical: number | null;
  alarms_warning: number | null;
}

// ── Проект: интеграции, модули, git, документы ───────────────────────

export type IntegrationStatus = 'observed' | 'online' | 'offline' | 'paused' | 'files-present' | 'cli-present'
  | 'needs-setup' | 'board-complete' | 'unavailable';

export interface Integration {
  id: string;
  name: string;
  group: string;
  status: IntegrationStatus | (string & {});
  detail: string;
  source: string;
  url?: string;
}

export interface ModuleInfo {
  id: string;
  name: string;
  description: string;
  files: string[];
  present: boolean;
  status: 'code-present' | 'incomplete' | (string & {});
}

export interface GitSummary {
  available: boolean;
  branch?: string | null;
  commit?: string | null;
  changed_files?: number;
  remote_count?: number;
  github_cli_available?: boolean;
}

export interface InsightDoc {
  path: string;
  title: string;
  status: string | null;
  updated_at: Iso;
}

export interface IncidentHead {
  title: string;
  source: string;
}

// ── Новое (§1): источники, капитал, спарклайны, позиции, события ─────

export interface SourceHealth {
  /** false — источник не прочитан (нет файла/исключение). */
  ok: boolean;
  /** Возраст данных в секундах; null — неизвестно. */
  age_s?: number | null;
  points?: number;
}

export type SourceName = 'risk' | 'engine' | 'equity' | 'positions' | 'events' | 'guard';
export type Sources = Partial<Record<SourceName, SourceHealth>>;

export type RangeKey = '24h' | '48h' | '7d' | 'all';
export const RANGE_KEYS: readonly RangeKey[] = ['24h', '48h', '7d', 'all'];

export interface EquityBlock {
  current: number | null;
  /** Внесённые деньги (lifetime.base_usdt); null — истории нет. */
  baseline_usdt: number | null;
  hwm: number | null;
  /** ≤ 300 точек на диапазон; < 2 точек → []. */
  ranges: Partial<Record<RangeKey, Series>>;
  range_hours: Partial<Record<RangeKey, number | null>>;
}

export interface Sparks {
  /** Капитал за 24 ч (≤ 40 точек). */
  equity: Series;
  /** equity − day_start_equity за риск-сутки. */
  day_pnl: Series;
  /** Просадка от скользящего максимума, положительные %, 7 суток. */
  drawdown_pct: Series;
  /** Накопленная прибыль без внешних потоков, вся история. */
  lifetime_net: Series;
}

export type PositionSource = 'engine' | 'pump' | 'risk';
export type PositionSide = 'long' | 'short' | 'net' | 'spot';
export type StopBasis = 'mark' | 'entry';

export interface Position {
  id: string;
  source: PositionSource | (string & {});
  symbol: string;
  side: PositionSide | (string & {});
  size: number | null;
  entry: number | null;
  /** Только если выводится честно; иначе null. */
  mark: number | null;
  upl: number | null;
  upl_pct: number | null;
  notional: number | null;
  stop: number | null;
  stop_distance_pct: number | null;
  stop_basis: StopBasis | null;
  liq_px: number | null;
  opened_at: Iso | null;
  age_s: number | null;
  updated_at: Iso | null;
}

export interface PositionsBlock {
  ok: boolean;
  count: number;
  total_upl: number | null;
  note: string | null;
  items: Position[];
}

export type EventKind = 'order' | 'fill' | 'stop' | 'breaker' | 'kill' | 'guard' | 'task' | 'incident' | 'alert' | 'engine' | 'action';
export type EventSeverity = 'info' | 'warn' | 'crit';

export interface ProjectEvent {
  /** ISO UTC. */
  ts: Iso;
  kind: EventKind | (string & {});
  severity: EventSeverity | (string & {});
  /** ≤ 120 символов, redacted. */
  title: string;
  /** ≤ 240 символов или null. */
  detail: string | null;
  /** Имя таблицы/файла, без путей с секретами. */
  source: string;
}

export interface EventsBlock {
  /** false — не прочитан ни один источник. */
  ok: boolean;
  sources: string[];
  /** Новые сверху, ≤ 100. */
  items: ProjectEvent[];
  /** Частичный сбой источников (имя: тип ошибки). */
  errors?: string[];
}

// ── Новое (§2): управление ───────────────────────────────────────────

export type ActionId = 'kill.engage' | 'orders.cancel_all' | 'engine.pause' | 'engine.resume'
  | 'breaker.reset.daily' | 'breaker.reset.global' | 'kill.reset';
export const ACTION_IDS: readonly ActionId[] = ['kill.engage', 'orders.cancel_all', 'engine.pause', 'engine.resume',
  'breaker.reset.daily', 'breaker.reset.global', 'kill.reset'];
export type ActionGroup = 'safety' | 'engine' | 'reset';
export type ActionLevel = 'emergency' | 'caution' | 'critical-reset';

export interface ControlAction {
  id: ActionId;
  label: string;
  group: ActionGroup;
  level: ActionLevel;
  /** 2000 для kill/cancel/engine; 2500 для reset. */
  hold_ms: number;
  /** Для reset — фраза, которую человек вводит текстом. */
  confirm_phrase: string | null;
  available: boolean;
  /** Почему недоступно: «kill-switch не активен», «движок уже остановлен»… */
  reason: string | null;
  warning: string | null;
}

export interface Controls {
  mode: 'demo' | (string & {});
  /** false — режим не demo или модуль действий недоступен. */
  enabled: boolean;
  actions: ControlAction[];
}

// ── Состояние целиком: GET /api/project/state ────────────────────────

export interface ProjectState {
  schema_version: 1;
  generated_at: Iso;
  mode: 'demo' | (string & {});
  data_quality: 'local-snapshot' | (string & {});
  refresh_seconds: number;
  board: Board;
  /** Задачи со статусом in-progress. */
  claims: BoardTask[];
  runtimes: Runtime[];
  queue: Queue;
  engine: EngineState | null;
  risk: RiskState | null;
  flags: Flags | null;
  pump: PumpState | null;
  /** Капитал за 48 ч (совместимость). */
  equity_history: Series;
  lifetime: Lifetime;
  ops: OpsState;
  netdata: Netdata | null;
  integrations: Integration[];
  modules: ModuleInfo[];
  git: GitSummary;
  insights: InsightDoc[];
  incidents: IncidentHead[];
  pending_decisions: string[];
  /** «имя: ТипОшибки» — без содержимого исключений. */
  errors: string[];

  // Аддитивные поля §1–2 (schema_version остаётся 1)
  sources?: Sources;
  equity?: EquityBlock;
  spark?: Sparks;
  spark_note?: string | null;
  positions?: PositionsBlock;
  events?: EventsBlock;
  controls?: Controls;
}

// ── POST-роуты управления ────────────────────────────────────────────

export interface ArmRequest { action: ActionId }

export interface ArmResponse {
  arm_id: string;
  action: ActionId;
  min_hold_ms: number;
  expires_in_s: number;
  confirm_phrase: string | null;
}

export interface CommitRequest { arm_id: string; confirm?: string }

export interface CommitResponse {
  ok: boolean;
  action: ActionId;
  summary: string;
  details?: unknown;
}

/** Коды ошибок POST-роутов (§2). */
export type ActionErrorCode = 'invalid-action' | 'unavailable' | 'busy' | 'hold-too-short' | 'arm-expired'
  | 'confirm-mismatch' | 'unknown-arm' | 'action-failed';

/** Тело ошибки любого роута /api/project/*. */
export interface ApiErrorBody {
  error: ActionErrorCode | 'morphy-login-required' | 'local-origin-required' | 'read-only'
    | 'project-data-unavailable' | 'task-unavailable' | 'invalid-task-id' | (string & {});
  reason?: string;
  summary?: string;
}
