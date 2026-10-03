// Обёртка fetch для /app/api/project/*: Bearer-сессия Morphy, таймауты, типизированные ошибки.
// GET-ошибки сохраняют прежние коды экрана: 401 → login, 404 → not-found, остальное → unavailable.
import type { ActionId, ApiErrorBody, ArmResponse, CommitResponse, ProjectState, TaskDetail } from './types';

export const API_BASE = '/app/api/project/';
export const API_TIMEOUT_MS = 15_000;
/** Commit ждёт выполнения действия: сервер даёт Python до 120 с. */
export const COMMIT_TIMEOUT_MS = 130_000;
export const TOKEN_KEY = 'bloby_token';

/**
 * Код ошибки:
 * - `login` — нет сессии Morphy (401);
 * - `not-found` — 404 у GET;
 * - `unavailable` — сервер/сеть недоступны, таймаут, прочие статусы GET;
 * - `aborted` — запрос отменил вызывающий (размонтирование, новый запрос);
 * - для POST — код из тела ответа сервера (`hold-too-short`, `busy`, `unavailable`, …).
 */
export type ApiErrorCode = 'login' | 'not-found' | 'unavailable' | 'aborted' | ApiErrorBody['error'];

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number | null;
  readonly body: ApiErrorBody | null;
  readonly timedOut: boolean;

  constructor(code: ApiErrorCode, status: number | null = null, body: ApiErrorBody | null = null, timedOut = false) {
    // message = код: старый экран делает setError(e.message) и сравнивает с 'login'.
    super(code);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.body = body;
    this.timedOut = timedOut;
  }

  /** Причина от сервера (409 unavailable → «kill-switch не активен»). */
  get reason(): string | null {
    return this.body?.reason ?? this.body?.summary ?? null;
  }
}

export function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export interface RequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

/** Сигнал, который срабатывает по таймауту или по внешней отмене. */
function linkSignals(external: AbortSignal | undefined, timeoutMs: number): { signal: AbortSignal; timedOut: () => boolean; dispose: () => void } {
  const controller = new AbortController();
  let expired = false;
  const timer = setTimeout(() => { expired = true; controller.abort(); }, timeoutMs);
  const onAbort = () => controller.abort();
  if (external) {
    if (external.aborted) onAbort();
    else external.addEventListener('abort', onAbort, { once: true });
  }
  return {
    signal: controller.signal,
    timedOut: () => expired,
    dispose: () => {
      clearTimeout(timer);
      external?.removeEventListener('abort', onAbort);
    },
  };
}

async function readErrorBody(response: Response): Promise<ApiErrorBody | null> {
  try {
    const body = await response.json();
    return body && typeof body === 'object' && typeof body.error === 'string' ? body as ApiErrorBody : null;
  } catch {
    return null;
  }
}

async function request<T>(method: 'GET' | 'POST', path: string, payload: unknown, options: RequestOptions): Promise<T> {
  const token = readToken();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (method === 'POST') headers['Content-Type'] = 'application/json';
  // Таймаут покрывает и заголовки, и чтение тела ответа.
  const linked = linkSignals(options.signal, options.timeoutMs ?? API_TIMEOUT_MS);
  const failed = () => {
    const aborted = options.signal?.aborted === true;
    return new ApiError(aborted ? 'aborted' : 'unavailable', null, null, !aborted && linked.timedOut());
  };
  try {
    let response: Response;
    try {
      response = await fetch(API_BASE + path, {
        method,
        headers,
        body: method === 'POST' ? JSON.stringify(payload ?? {}) : undefined,
        signal: linked.signal,
        cache: 'no-store',
        credentials: 'same-origin',
      });
    } catch {
      throw failed();
    }
    if (response.ok) {
      try {
        return await response.json() as T;
      } catch {
        if (linked.signal.aborted) throw failed();
        throw new ApiError('unavailable', response.status);
      }
    }
    const body = await readErrorBody(response);
    if (response.status === 401) throw new ApiError('login', 401, body);
    if (method === 'GET') throw new ApiError(response.status === 404 ? 'not-found' : 'unavailable', response.status, body);
    throw new ApiError(body?.error ?? (response.status === 404 ? 'not-found' : 'unavailable'), response.status, body);
  } finally {
    linked.dispose();
  }
}

export function apiGet<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return request<T>('GET', path, undefined, options);
}

export function apiPost<T>(path: string, payload: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>('POST', path, payload, options);
}

// ── Роуты экрана проекта ─────────────────────────────────────────────

export function fetchState(signal?: AbortSignal): Promise<ProjectState> {
  return apiGet<ProjectState>('state', { signal });
}

export function fetchTask(id: string, signal?: AbortSignal): Promise<TaskDetail> {
  return apiGet<TaskDetail>('task/' + encodeURIComponent(id), { signal });
}

/** Шаг 1 удержания: сервер выдаёт одноразовый arm_id и минимальное время удержания. */
export function armAction(action: ActionId, signal?: AbortSignal): Promise<ArmResponse> {
  return apiPost<ArmResponse>('action/arm', { action }, { signal });
}

/** Шаг 2: подтверждение после удержания (и фразы для reset). Ответ — только краткий summary. */
export function commitAction(armId: string, confirm?: string, signal?: AbortSignal): Promise<CommitResponse> {
  const payload = confirm == null ? { arm_id: armId } : { arm_id: armId, confirm };
  return apiPost<CommitResponse>('action/commit', payload, { signal, timeoutMs: COMMIT_TIMEOUT_MS });
}

/** Понятный человеку текст ошибки. */
export function describeApiError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Неизвестная ошибка запроса.';
  switch (error.code) {
    case 'login': return 'Нужен вход в Morphy.';
    case 'not-found': return 'Данные не найдены.';
    case 'aborted': return 'Запрос отменён.';
    case 'unavailable':
      if (error.timedOut) return 'Сервер не ответил за отведённое время.';
      return error.reason ? `Недоступно: ${error.reason}.` : 'Сервер Morphy недоступен.';
    case 'invalid-action': return 'Неизвестное действие.';
    case 'busy': return 'Другое действие ещё выполняется. Подожди и повтори.';
    case 'hold-too-short': return 'Удержание короче требуемого — действие не выполнено.';
    case 'arm-expired': return 'Подтверждение устарело. Начни заново.';
    case 'confirm-mismatch': return 'Фраза подтверждения не совпала.';
    case 'unknown-arm': return 'Подтверждение не найдено. Начни заново.';
    case 'action-failed': return error.reason ? `Действие не выполнено: ${error.reason}` : 'Действие не выполнено.';
    default: return `Ошибка: ${error.code}.`;
  }
}
