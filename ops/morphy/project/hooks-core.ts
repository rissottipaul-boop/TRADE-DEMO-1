// Чистое ядро хуков: разбор горячих клавиш и состояние в URL. Без React и DOM-глобалей —
// модуль напрямую грузит `node --test` (hooks-core.test.mjs).

// ── Горячие клавиши ──────────────────────────────────────────────────

export interface Hotkey {
  /** Клавиша в нижнем регистре для букв; «/», «?», «1», «escape»… */
  key: string;
  ctrl: boolean;
  alt: boolean;
  meta: boolean;
  /** Ctrl на Windows/Linux, ⌘ на macOS. */
  mod: boolean;
  shift: boolean;
}

/** Поля KeyboardEvent, которые нужны сопоставлению (удобно для тестов). */
export interface KeyLike {
  key: string;
  code?: string;
  ctrlKey?: boolean;
  altKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
}

/** «r», «1», «/», «?», «Escape», «shift+r», «mod+k», «ctrl+alt+x». */
export function parseHotkey(spec: string): Hotkey {
  const parts = spec.split('+');
  // «shift++» или просто «+» — клавиша «+».
  let key = parts.pop() ?? '';
  if (key === '' && spec.endsWith('+')) { key = '+'; if (parts[parts.length - 1] === '') parts.pop(); }
  const mods = new Set(parts.map((p) => p.trim().toLowerCase()).filter(Boolean));
  for (const mod of mods) {
    if (!['ctrl', 'alt', 'meta', 'mod', 'shift'].includes(mod)) throw new Error(`Неизвестный модификатор: ${mod}`);
  }
  if (!key) throw new Error(`Пустая клавиша: «${spec}»`);
  return {
    key: key.toLowerCase(),
    ctrl: mods.has('ctrl'),
    alt: mods.has('alt'),
    meta: mods.has('meta'),
    mod: mods.has('mod'),
    shift: mods.has('shift'),
  };
}

const LETTER = /^[a-z]$/;
const DIGIT = /^[0-9]$/;
const ALNUM = /^[a-z0-9]$/;

/** Физическая клавиша US-раскладки для символа: работает и при русской раскладке. */
const CODE_FALLBACK: Record<string, { code: string; shift: boolean }> = {
  '/': { code: 'Slash', shift: false },
  '?': { code: 'Slash', shift: true },
  '[': { code: 'BracketLeft', shift: false },
  ']': { code: 'BracketRight', shift: false },
  ',': { code: 'Comma', shift: false },
  '.': { code: 'Period', shift: false },
  '-': { code: 'Minus', shift: false },
  '=': { code: 'Equal', shift: false },
};

/**
 * Совпадает ли нажатие с горячей клавишей.
 * Ctrl/Alt/Meta должны совпасть точно (Ctrl+R браузера не перехватывается клавишей «r»).
 * Буквы, цифры и именованные клавиши требуют точного Shift; символ, который сам набирается
 * с Shift («?»), Shift не проверяет. Буквы/цифры/символы сверяются и по event.code — поэтому
 * «r», «1» и «/» работают при русской раскладке.
 */
export function matchHotkey(event: KeyLike, hotkey: Hotkey | string, isMac = false): boolean {
  const spec = typeof hotkey === 'string' ? parseHotkey(hotkey) : hotkey;
  const wantCtrl = spec.ctrl || (spec.mod && !isMac);
  const wantMeta = spec.meta || (spec.mod && isMac);
  if (!!event.ctrlKey !== wantCtrl || !!event.metaKey !== wantMeta || !!event.altKey !== spec.alt) return false;
  const key = (event.key || '').toLowerCase();
  const code = event.code || '';
  const shift = !!event.shiftKey;
  if (LETTER.test(spec.key)) {
    if (shift !== spec.shift) return false;
    return key === spec.key || code === 'Key' + spec.key.toUpperCase();
  }
  if (DIGIT.test(spec.key)) {
    if (shift !== spec.shift) return false;
    return key === spec.key || code === 'Digit' + spec.key || code === 'Numpad' + spec.key;
  }
  if (spec.key.length === 1 && !ALNUM.test(spec.key)) {
    if (key === spec.key) return spec.shift ? shift : true;
    const fallback = CODE_FALLBACK[spec.key];
    return !!fallback && code === fallback.code && shift === (fallback.shift || spec.shift);
  }
  // Именованные клавиши: Escape, Enter, ArrowLeft…
  if (shift !== spec.shift) return false;
  return key === spec.key || (spec.key === 'esc' && key === 'escape');
}

/** Цель — поле ввода? Тогда одиночные горячие клавиши не срабатывают. */
export function isEditableTarget(target: unknown): boolean {
  if (!target || typeof target !== 'object') return false;
  const el = target as { tagName?: string; type?: string; isContentEditable?: boolean };
  if (el.isContentEditable) return true;
  const tag = (el.tagName || '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (el.type || 'text').toLowerCase();
    return !['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'color', 'file', 'image'].includes(type);
  }
  return false;
}

// ── Состояние в URL ──────────────────────────────────────────────────

export type UrlDefaults<K extends string> = Record<K, string>;
export type UrlAllowed<K extends string> = Partial<Record<K, readonly string[]>>;
export type UrlPatch<K extends string> = Partial<Record<K, string | null>>;

/** Значения ключей из строки запроса; нет ключа или значение вне allowed → значение по умолчанию. */
export function readUrlState<K extends string>(search: string, defaults: UrlDefaults<K>, allowed: UrlAllowed<K> = {}): Record<K, string> {
  const params = new URLSearchParams(search);
  const state = {} as Record<K, string>;
  for (const key of Object.keys(defaults) as K[]) {
    const value = params.get(key);
    const ok = value != null && value !== '' && (!allowed[key] || allowed[key]!.includes(value));
    state[key] = ok ? value! : defaults[key];
  }
  return state;
}

/**
 * Новая строка запроса после патча. Значение null или равное умолчанию удаляет ключ
 * (короткие ссылки); чужие параметры сохраняются. Возвращает «?a=1» или «».
 */
export function writeUrlState<K extends string>(search: string, patch: UrlPatch<K>, defaults: UrlDefaults<K>): string {
  const params = new URLSearchParams(search);
  for (const key of Object.keys(patch) as K[]) {
    const value = patch[key];
    if (value == null || value === '' || value === defaults[key]) params.delete(key);
    else params.set(key, value);
  }
  const text = params.toString();
  return text ? '?' + text : '';
}
