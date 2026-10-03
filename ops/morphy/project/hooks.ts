// React-хуки экрана проекта: живое «сейчас», состояние в URL, горячие клавиши, опрос сервера.
// Чистая логика (разбор клавиш, сборка строки запроса) — в hooks-core.ts.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { isEditableTarget, matchHotkey, parseHotkey, readUrlState, writeUrlState } from './hooks-core';
import type { Hotkey, UrlAllowed, UrlDefaults, UrlPatch } from './hooks-core';

export { isEditableTarget, matchHotkey, parseHotkey } from './hooks-core';

// ── Видимость вкладки и медиа-запросы ────────────────────────────────

function subscribeVisibility(onChange: () => void): () => void {
  document.addEventListener('visibilitychange', onChange);
  return () => document.removeEventListener('visibilitychange', onChange);
}

/** true, пока вкладка браузера скрыта. */
export function useDocumentHidden(): boolean {
  return useSyncExternalStore(subscribeVisibility, () => document.visibilityState === 'hidden', () => false);
}

/** Совпадение медиа-запроса (обновляется при изменении). */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    if (typeof window === 'undefined' || !window.matchMedia) return () => {};
    const list = window.matchMedia(query);
    list.addEventListener('change', onChange);
    return () => list.removeEventListener('change', onChange);
  }, [query]);
  return useSyncExternalStore(subscribe, () => !!window.matchMedia?.(query).matches, () => false);
}

/** Пользователь просит меньше анимации — отключаем кольца/мерцание на JS. */
export function useReducedMotion(): boolean {
  return useMediaQuery('(prefers-reduced-motion: reduce)');
}

// ── Живое «сейчас» ───────────────────────────────────────────────────

/**
 * Текущее время в мс, обновляется раз в `intervalMs` (для «12 сек назад»).
 * На скрытой вкладке не тикает; при возвращении сразу обновляется.
 */
export function useNow(intervalMs = 1000, options: { pauseWhenHidden?: boolean } = {}): number {
  const pauseWhenHidden = options.pauseWhenHidden ?? true;
  const hidden = useDocumentHidden();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (pauseWhenHidden && hidden) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), Math.max(250, intervalMs));
    return () => clearInterval(timer);
  }, [intervalMs, hidden, pauseWhenHidden]);
  return now;
}

// ── Состояние в URL ──────────────────────────────────────────────────

export interface UrlStateSetOptions {
  /** true — заменить запись истории (ввод в поиск), false — новая запись (смена вкладки). */
  replace?: boolean;
}

export type SetUrlState<K extends string> = (patch: UrlPatch<K>, options?: UrlStateSetOptions) => void;

/**
 * Синхронизация ключей (section/q/status…) со строкой запроса через react-router.
 * Значение по умолчанию в URL не пишется; значение вне `allowed` читается как умолчание.
 * Чужие параметры строки запроса сохраняются. `defaults`/`allowed` должны быть стабильны
 * по смыслу (берутся при каждом рендере, но не являются зависимостями).
 */
export function useUrlState<K extends string>(defaults: UrlDefaults<K>, allowed: UrlAllowed<K> = {}): [Record<K, string>, SetUrlState<K>] {
  const location = useLocation();
  const navigate = useNavigate();
  const defaultsRef = useRef(defaults);
  const allowedRef = useRef(allowed);
  defaultsRef.current = defaults;
  allowedRef.current = allowed;
  // База для серии записей до перерисовки (быстрый ввод в поиск).
  const searchRef = useRef(location.search);
  const locationRef = useRef(location);
  searchRef.current = location.search;
  locationRef.current = location;

  const state = useMemo(
    () => readUrlState(location.search, defaultsRef.current, allowedRef.current),
    [location.search],
  );

  const set = useCallback<SetUrlState<K>>((patch, options = {}) => {
    const search = writeUrlState(searchRef.current, patch, defaultsRef.current);
    if (search === searchRef.current) return;
    searchRef.current = search;
    const { pathname, hash } = locationRef.current;
    navigate({ pathname, search, hash }, { replace: options.replace ?? false });
  }, [navigate]);

  return [state, set];
}

// ── Горячие клавиши ──────────────────────────────────────────────────

export type HotkeyHandler = (event: KeyboardEvent) => void;

export interface HotkeyOptions {
  enabled?: boolean;
  /** Клавиши, которые работают и в полях ввода (обычно только «Escape»). */
  allowInInputs?: readonly string[];
  /** Повтор при удержании клавиши (по умолчанию нет: «R» не шлёт десяток обновлений). */
  allowRepeat?: boolean;
}

/**
 * Глобальные горячие клавиши: { r: refresh, '1': () => setTab(…), '/': focusSearch, '?': help }.
 * Не срабатывают в input/textarea/select/contenteditable (кроме allowInInputs), при IME-наборе
 * и при несовпадающих модификаторах. Совпадение отменяет действие браузера по умолчанию.
 */
export function useHotkeys(bindings: Record<string, HotkeyHandler>, options: HotkeyOptions = {}): void {
  const { enabled = true, allowRepeat = false } = options;
  const bindingsRef = useRef(bindings);
  bindingsRef.current = bindings;
  const allowRef = useRef(options.allowInInputs ?? ['Escape']);
  allowRef.current = options.allowInInputs ?? ['Escape'];
  const specsKey = Object.keys(bindings).join('\u0000');
  const parsed = useMemo(
    () => specsKey.split('\u0000').filter(Boolean).map((spec): [string, Hotkey] => [spec, parseHotkey(spec)]),
    [specsKey],
  );

  useEffect(() => {
    if (!enabled) return;
    const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || (event.repeat && !allowRepeat)) return;
      const editable = isEditableTarget(event.target);
      for (const [spec, hotkey] of parsed) {
        if (!matchHotkey(event, hotkey, isMac)) continue;
        if (editable && !allowRef.current.some((allowedSpec) => allowedSpec.toLowerCase() === spec.toLowerCase())) return;
        const handler = bindingsRef.current[spec];
        if (!handler) return;
        event.preventDefault();
        handler(event);
        return;
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [enabled, allowRepeat, parsed]);
}

// ── Опрос сервера ────────────────────────────────────────────────────

export interface PollingOptions {
  /** Ручная пауза авторефреша (кнопка «пауза»). Ручной refresh() работает и на паузе. */
  paused?: boolean;
  /** Не опрашивать скрытую вкладку (по умолчанию да); при возвращении — сразу, если просрочено. */
  pauseWhenHidden?: boolean;
  /** Первый запуск сразу при монтировании (по умолчанию да). */
  immediate?: boolean;
}

export interface Polling {
  /** Запустить сейчас; если запрос уже идёт — вернуть его. Таймер перезапускается. */
  refresh: () => Promise<void>;
  /** Идёт запрос. */
  running: boolean;
  /** Время окончания последнего запуска (мс) или null. */
  lastRunAt: number | null;
  /** Когда запланирован следующий запуск (мс); null — на паузе. */
  nextRunAt: number | null;
  /** Опрос остановлен: ручная пауза или скрытая вкладка. */
  paused: boolean;
  hidden: boolean;
}

/**
 * Периодический запуск `task` через `intervalMs` после окончания предыдущего (без наложений).
 * Ошибки — забота task (он сам ставит состояние ошибки); опрос после ошибки продолжается.
 * При размонтировании текущий запрос отменяется через AbortSignal.
 */
export function usePolling(task: (signal: AbortSignal) => unknown, intervalMs: number, options: PollingOptions = {}): Polling {
  const { paused = false, pauseWhenHidden = true, immediate = true } = options;
  const hidden = useDocumentHidden();
  const stopped = paused || (pauseWhenHidden && hidden);
  const taskRef = useRef(task);
  taskRef.current = task;
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const mountedAt = useRef(Date.now());
  const [running, setRunning] = useState(false);
  const [lastRunAt, setLastRunAt] = useState<number | null>(null);
  const [nextRunAt, setNextRunAt] = useState<number | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);

  const refresh = useCallback((): Promise<void> => {
    if (inFlight.current) return inFlight.current;
    const abort = new AbortController();
    controller.current = abort;
    setRunning(true);
    const run = (async () => {
      try {
        await taskRef.current(abort.signal);
      } catch {
        // Ошибку показывает сам task; опрос не останавливается.
      } finally {
        inFlight.current = null;
        if (mounted.current) {
          setRunning(false);
          setLastRunAt(Date.now());
        }
      }
    })();
    inFlight.current = run;
    return run;
  }, []);

  useEffect(() => {
    if (stopped) {
      setNextRunAt(null);
      return;
    }
    const base = lastRunAt ?? (immediate ? Date.now() - intervalMs : mountedAt.current);
    const due = base + Math.max(1000, intervalMs);
    setNextRunAt(due);
    const timer = setTimeout(() => { void refresh(); }, Math.max(0, due - Date.now()));
    return () => clearTimeout(timer);
  }, [stopped, intervalMs, lastRunAt, immediate, refresh]);

  return { refresh, running, lastRunAt, nextRunAt, paused: stopped, hidden };
}
