// Форматирование чисел, денег, процентов и времени для экрана проекта.
// Только чистые функции: «сейчас» передаётся аргументом (хук useNow), а не читается здесь.
// Язык — ru-RU, часовой пояс отображения — Asia/Qyzylorda.
// Модуль без импортов: его напрямую грузит `node --test` (format.test.mjs).

export const LOCALE = 'ru-RU';
export const TIME_ZONE = 'Asia/Qyzylorda';
/** Пустое значение: «нет данных», не ноль. */
export const NO_DATA = '—';
/** Типографский минус (U+2212): одинаковой ширины с плюсом в tabular-nums. */
export const MINUS = '−';
/** Неразрывный пробел между числом и единицей. */
export const NBSP = ' ';

export type Tone = 'neutral' | 'accent' | 'info' | 'ok' | 'pos' | 'neg' | 'warn' | 'crit' | 'none';

const numberFormats = new Map<string, Intl.NumberFormat>();
function numberFormat(min: number, max: number): Intl.NumberFormat {
  const key = `${min}:${max}`;
  let fmt = numberFormats.get(key);
  if (!fmt) {
    fmt = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: min, maximumFractionDigits: max });
    numberFormats.set(key, fmt);
  }
  return fmt;
}

/** Конечное число? (null/undefined/NaN/Infinity/строки — нет). */
export function isNum(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

export interface NumberOptions {
  /** Минимум знаков после запятой (по умолчанию 0 — хвостовые нули срезаются). */
  minDigits?: number;
  /** Показывать «+» у положительных. */
  sign?: boolean;
}

/**
 * Число в ru-RU: «109 433,21». Неконечное/не число → «—».
 * `digits` — максимум знаков после запятой (как прежний `number(n, digits)`).
 */
export function fmtNumber(value: unknown, digits = 2, options: NumberOptions = {}): string {
  if (!isNum(value)) return NO_DATA;
  const max = Math.max(0, digits);
  const min = Math.min(max, Math.max(0, options.minDigits ?? 0));
  const text = numberFormat(min, max).format(Math.abs(value));
  // Знак считаем по округлённому значению: −0,001 при 2 знаках — это «0», без минуса.
  const rounded = Number(Math.abs(value).toFixed(max));
  if (rounded === 0) return text;
  if (value < 0) return MINUS + text;
  return options.sign ? '+' + text : text;
}

/** Целое число: «1 730». */
export function fmtInt(value: unknown): string {
  return fmtNumber(isNum(value) ? Math.round(value) : value, 0);
}

export interface MoneyOptions {
  /** Знаков после запятой (фиксировано, по умолчанию 2). */
  digits?: number;
  sign?: boolean;
  /** Единица после числа; null — без единицы. По умолчанию «USDT». */
  unit?: string | null;
}

export interface SplitNumber {
  /** Полная строка, как её прочитает скринридер. */
  text: string;
  /** Знак и целая часть с разрядами: «+109 433». Для «нет данных» — «—». */
  int: string;
  /** Дробная часть с запятой: «,21»; пусто, если её нет. */
  frac: string;
  /** Единица с неразрывным пробелом впереди: « USDT»; пусто, если нет. */
  unit: string;
  /** false — значение отсутствует (рисовать как «нет данных»). */
  present: boolean;
}

/** Делит отформатированное число на целую и дробную часть по десятичной запятой. */
export function splitDecimals(formatted: string): { int: string; frac: string } {
  const index = formatted.lastIndexOf(',');
  if (index < 0) return { int: formatted, frac: '' };
  return { int: formatted.slice(0, index), frac: formatted.slice(index) };
}

/** Деньги по частям для приглушённых десятых: { int: "109 433", frac: ",21", unit: " USDT" }. */
export function splitMoney(value: unknown, options: MoneyOptions = {}): SplitNumber {
  const digits = options.digits ?? 2;
  const unit = options.unit === undefined ? 'USDT' : options.unit;
  const unitText = unit ? NBSP + unit : '';
  if (!isNum(value)) return { text: NO_DATA, int: NO_DATA, frac: '', unit: '', present: false };
  const number = fmtNumber(value, digits, { minDigits: digits, sign: options.sign });
  const { int, frac } = splitDecimals(number);
  return { text: number + unitText, int, frac, unit: unitText, present: true };
}

/** Деньги одной строкой: «+5 467,66 USDT». */
export function fmtMoney(value: unknown, options: MoneyOptions = {}): string {
  return splitMoney(value, options).text;
}

export interface PercentOptions {
  /** Знаков после запятой (по умолчанию 2). */
  digits?: number;
  sign?: boolean;
  /** true (по умолчанию) — фиксированное число знаков: ширина не прыгает при рефреше. */
  fixed?: boolean;
}

/** Процент от значения в процентах (не доле): 2.48 → «2,48%». */
export function fmtPercent(value: unknown, options: PercentOptions = {}): string {
  if (!isNum(value)) return NO_DATA;
  const digits = options.digits ?? 2;
  const fixed = options.fixed ?? true;
  return fmtNumber(value, digits, { minDigits: fixed ? digits : 0, sign: options.sign }) + '%';
}

/** Тон по знаку: >0 — pos, <0 — neg, 0 — neutral, нет данных — none (НЕ зелёный). */
export function signTone(value: unknown): Tone {
  if (!isNum(value)) return 'none';
  if (value > 0) return 'pos';
  if (value < 0) return 'neg';
  return 'neutral';
}

/**
 * Тон порогового индикатора: доля value/limit < warnAt — ok, ≤ critAt — warn, выше — crit.
 * Нет значения или лимита → none.
 */
export function thresholdTone(value: unknown, limit: unknown, warnAt = 0.6, critAt = 0.85): Tone {
  if (!isNum(value) || !isNum(limit) || limit <= 0) return 'none';
  const share = value / limit;
  if (share < warnAt) return 'ok';
  if (share <= critAt) return 'warn';
  return 'crit';
}

// ── Время ─────────────────────────────────────────────────────────────

/** Метка времени в миллисекундах: число — unix-секунды (≥ 1e11 считается мс), строка — ISO. */
export function toMs(ts: unknown): number | null {
  if (isNum(ts)) return ts >= 1e11 ? ts : ts * 1000;
  if (typeof ts === 'string' && ts) {
    const ms = Date.parse(ts);
    return Number.isFinite(ms) ? ms : null;
  }
  return null;
}

const dateFormats = new Map<string, Intl.DateTimeFormat>();
function dateFormat(key: string, options: Intl.DateTimeFormatOptions, locale = LOCALE): Intl.DateTimeFormat {
  let fmt = dateFormats.get(key);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat(locale, { timeZone: TIME_ZONE, ...options });
    dateFormats.set(key, fmt);
  }
  return fmt;
}

/** «03.10, 09:12» — как прежний `time()`. */
export function fmtTime(ts: unknown): string {
  const ms = toMs(ts);
  return ms == null ? NO_DATA : dateFormat('time', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(ms);
}

/** «03.10» — как прежний `day()`. */
export function fmtDay(ts: unknown): string {
  const ms = toMs(ts);
  return ms == null ? NO_DATA : dateFormat('day', { day: '2-digit', month: '2-digit' }).format(ms);
}

/** «09:12» */
export function fmtClock(ts: unknown, seconds = false): string {
  const ms = toMs(ts);
  if (ms == null) return NO_DATA;
  return seconds
    ? dateFormat('clock-s', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(ms)
    : dateFormat('clock', { hour: '2-digit', minute: '2-digit' }).format(ms);
}

/** «03.10.2026, 09:12» — полная дата для подсказок. */
export function fmtDateTime(ts: unknown): string {
  const ms = toMs(ts);
  return ms == null ? NO_DATA : dateFormat('datetime', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }).format(ms);
}

/** Ключ дня в поясе отображения: «2026-10-03» (для группировки ленты событий). */
export function dayKey(ts: unknown): string | null {
  const ms = toMs(ts);
  return ms == null ? null : dateFormat('daykey', { year: 'numeric', month: '2-digit', day: '2-digit' }, 'en-CA').format(ms);
}

/** Заголовок дня: «Сегодня», «Вчера», «1 октября». */
export function fmtDayLabel(ts: unknown, nowMs: number): string {
  const key = dayKey(ts);
  if (!key) return NO_DATA;
  if (key === dayKey(nowMs)) return 'Сегодня';
  if (key === dayKey(nowMs - 86_400_000)) return 'Вчера';
  return dateFormat('daylabel', { day: 'numeric', month: 'long' }).format(toMs(ts) as number);
}

/** Склонение: plural(3, ['день', 'дня', 'дней']) → «дня». */
export function plural(n: number, forms: readonly [string, string, string]): string {
  const abs = Math.abs(Math.trunc(n));
  const mod10 = abs % 10;
  const mod100 = abs % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

const DAYS = ['день', 'дня', 'дней'] as const;

/** Возраст в секундах → «12 сек назад», «3 мин назад», «2 ч назад», «3 дня назад». */
export function fmtAgo(seconds: unknown): string {
  if (!isNum(seconds)) return NO_DATA;
  if (seconds < 5) return 'только что';
  if (seconds < 60) return `${Math.floor(seconds)} сек назад`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} ч назад`;
  const days = Math.floor(seconds / 86_400);
  return `${days} ${plural(days, DAYS)} назад`;
}

/** Относительное время метки: прошлое — «3 мин назад», будущее — «через 3 мин». */
export function fmtRelative(ts: unknown, nowMs: number): string {
  const ms = toMs(ts);
  if (ms == null || !isNum(nowMs)) return NO_DATA;
  const delta = (nowMs - ms) / 1000;
  if (delta > -5) return fmtAgo(Math.max(0, delta));
  return 'через ' + fmtDuration(-delta);
}

/** Длительность: «45 сек», «12 мин», «14 ч», «1 д 15 ч». */
export function fmtDuration(seconds: unknown): string {
  if (!isNum(seconds) || seconds < 0) return NO_DATA;
  if (seconds < 60) return `${Math.floor(seconds)} сек`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} ч`;
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3600);
  return hours ? `${days} д ${hours} ч` : `${days} д`;
}

/** Возраст задачи в днях → «3 дня в работе», «5 ч в работе», «меньше часа в работе». */
export function fmtTaskAge(days: unknown, suffix = 'в работе'): string {
  if (!isNum(days) || days < 0) return NO_DATA;
  const tail = suffix ? ' ' + suffix : '';
  if (days < 1 / 24) return 'меньше часа' + tail;
  if (days < 1) return `${Math.floor(days * 24)} ч${tail}`;
  const whole = Math.floor(days);
  return `${whole} ${plural(whole, DAYS)}${tail}`;
}

/** Тон возраста задачи: амбер начиная с `warnAfterDays` суток, иначе нейтральный. */
export function ageTone(days: unknown, warnAfterDays = 3): Tone {
  if (!isNum(days)) return 'none';
  return days >= warnAfterDays ? 'warn' : 'neutral';
}

// ── Подписи статусов ──────────────────────────────────────────────────

export const TASK_STATUS_TITLES: Record<string, string> = {
  ready: 'Готово к работе',
  'in-progress': 'В работе',
  done: 'Выполнено',
  blocked: 'Заблокировано',
  'needs-user': 'Нужно решение',
  scheduled: 'По расписанию',
};

/** Зелёный — только деньги и здоровье, поэтому «выполнено» нейтрально. */
export function taskStatusTone(status: unknown): Tone {
  switch (status) {
    case 'ready': return 'accent';
    case 'in-progress': return 'info';
    case 'needs-user': return 'warn';
    case 'blocked': return 'warn';
    case 'done':
    case 'scheduled': return 'neutral';
    default: return 'none';
  }
}

export const INTEGRATION_TITLES: Record<string, string> = {
  observed: 'Данные доступны',
  online: 'Онлайн',
  offline: 'Офлайн',
  paused: 'На паузе',
  'files-present': 'Файлы доступны',
  'cli-present': 'CLI установлен',
  'needs-setup': 'Нужна настройка',
  'board-complete': 'Настройка отмечена',
  unavailable: 'Нет данных',
};

/** Статус интеграции → тон: работает — ok (здоровье), выключено — crit, нет данных — none. */
export function integrationTone(status: unknown): Tone {
  switch (status) {
    case 'online':
    case 'observed':
    case 'files-present':
    case 'cli-present': return 'ok';
    case 'needs-setup':
    case 'paused': return 'warn';
    case 'offline': return 'crit';
    case 'board-complete': return 'neutral';
    default: return 'none';
  }
}

/** Важность события ленты → тон. */
export function severityTone(severity: unknown): Tone {
  return severity === 'crit' ? 'crit' : severity === 'warn' ? 'warn' : severity === 'info' ? 'neutral' : 'none';
}
