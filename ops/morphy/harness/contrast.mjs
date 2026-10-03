// Проверка контраста дизайн-токенов экрана проекта (WCAG 2.x).
// Читает ops/morphy/project/tokens.css, вычисляет все пары из объявлений @contrast
// и падает (код 1), если хоть одна пара ниже порога или токен текста не покрыт парой.
//
//   node ops/morphy/harness/contrast.mjs [путь/к/tokens.css] [--json]
//
// Формат объявления (в комментарии tokens.css):
//   @contrast <text|large|ui> <цвета текста…> / <подложки…>
//   text = 4.5:1; large = 3:1 (крупный/неактивный текст); ui = 3:1 (нетекстовые элементы).
//   «--a@--b» — полупрозрачная подложка --a поверх --b (цепочки разрешены);
//   «--fg*--alpha» — цвет --fg с дополнительной прозрачностью из числового токена --alpha.
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const THRESHOLDS = { text: 4.5, large: 3, ui: 3 };

// Токены, которые являются цветом текста/значков и обязаны быть в какой-то паре.
const FOREGROUND = /^--pj-(text(-[a-z0-9]+)?|on-[a-z]+|money-(pos|neg)|ok|warn|crit|info|neutral|accent|accent-strong|focus|chart-axis)$/;

/** Убирает комментарии, возвращает текст и строки @contrast из комментариев. */
export function splitComments(css) {
  const rules = [];
  const text = css.replace(/\/\*[\s\S]*?\*\//g, (comment) => {
    for (const line of comment.split(/\r?\n/)) {
      // Только строки, начинающиеся с @contrast (после «*»): упоминания в тексте — не правила.
      const m = /^\s*(?:\/\*+|\*)?\s*@contrast\s+(.+)$/.exec(line);
      if (m) rules.push(m[1].replace(/\*\/\s*$/, '').trim());
    }
    return ' ';
  });
  return { text, rules };
}

/** Пользовательские свойства из блоков верхнего уровня с селектором ровно `.pj-root`. */
export function readTokens(css, selector = '.pj-root') {
  const { text } = splitComments(css);
  const tokens = new Map();
  let depth = 0;
  let start = 0;
  let current = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '{') {
      if (depth === 0) current = text.slice(start, i).trim();
      depth++;
      if (depth === 1) start = i + 1;
    } else if (ch === '}') {
      depth--;
      if (depth === 0) {
        if (current === selector) {
          for (const decl of text.slice(start, i).split(';')) {
            const m = /^\s*(--[a-z0-9-]+)\s*:\s*([\s\S]+?)\s*$/i.exec(decl);
            if (m) tokens.set(m[1], m[2]);
          }
        }
        current = null;
        start = i + 1;
      }
    }
  }
  return tokens;
}

/** Цвет из #hex / rgb() / rgba(); числа — как прозрачность. */
export function parseColor(value) {
  const v = value.trim().toLowerCase();
  let m = /^#([0-9a-f]{3,8})$/.exec(v);
  if (m) {
    let h = m[1];
    if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join('');
    if (h.length !== 6 && h.length !== 8) return null;
    const n = (k) => parseInt(h.slice(k, k + 2), 16);
    return { r: n(0), g: n(2), b: n(4), a: h.length === 8 ? n(6) / 255 : 1 };
  }
  m = /^rgba?\(([^)]+)\)$/.exec(v);
  if (m) {
    const parts = m[1].split(/[\s,/]+/).filter(Boolean);
    if (parts.length < 3) return null;
    const ch = (p) => (p.endsWith('%') ? (parseFloat(p) / 100) * 255 : parseFloat(p));
    const alpha = parts[3] === undefined ? 1 : parts[3].endsWith('%') ? parseFloat(parts[3]) / 100 : parseFloat(parts[3]);
    return { r: ch(parts[0]), g: ch(parts[1]), b: ch(parts[2]), a: alpha };
  }
  return null;
}

export function resolve(tokens, name, seen = new Set()) {
  if (seen.has(name)) throw new Error(`цикл var(): ${[...seen, name].join(' → ')}`);
  seen.add(name);
  const raw = tokens.get(name);
  if (raw === undefined) throw new Error(`токен ${name} не объявлен`);
  const ref = /^var\((--[a-z0-9-]+)\)$/i.exec(raw.trim());
  return ref ? resolve(tokens, ref[1], seen) : raw.trim();
}

function colorOf(tokens, name) {
  const value = resolve(tokens, name);
  const color = parseColor(value);
  if (!color) throw new Error(`${name}: «${value}» — не цвет`);
  return color;
}

/** Альфа-композиция top поверх непрозрачного bottom. */
export function over(top, bottom) {
  const a = top.a;
  return { r: top.r * a + bottom.r * (1 - a), g: top.g * a + bottom.g * (1 - a), b: top.b * a + bottom.b * (1 - a), a: 1 };
}

export function luminance({ r, g, b }) {
  const lin = (c) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export function ratio(fg, bg) {
  const [l1, l2] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

/** Подложка: «--a» или «--a@--b@--c» (каждая следующая — основание предыдущей). */
export function background(tokens, spec) {
  const chain = spec.split('@');
  let color = colorOf(tokens, chain[chain.length - 1]);
  if (color.a < 1) throw new Error(`${spec}: нижняя подложка полупрозрачна — укажи основание через @`);
  for (let i = chain.length - 2; i >= 0; i--) color = over(colorOf(tokens, chain[i]), color);
  return color;
}

/** Цвет текста: «--fg» или «--fg*--alpha». */
export function foreground(tokens, spec, bg) {
  const [name, alphaName] = spec.split('*');
  const color = { ...colorOf(tokens, name) };
  if (alphaName) {
    const alpha = parseFloat(resolve(tokens, alphaName));
    if (!(alpha > 0 && alpha <= 1)) throw new Error(`${alphaName}: прозрачность должна быть в (0, 1]`);
    color.a *= alpha;
  }
  return color.a < 1 ? over(color, bg) : color;
}

const hex = (c) => '#' + [c.r, c.g, c.b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');

export function check(css) {
  const tokens = readTokens(css);
  const { rules } = splitComments(css);
  const results = [];
  const errors = [];
  const covered = new Set();
  if (!rules.length) errors.push('нет ни одного объявления @contrast');
  for (const rule of rules) {
    const m = /^(text|large|ui)\s+(.+?)\s+\/\s+(.+)$/.exec(rule);
    if (!m) { errors.push(`не разобрано: @contrast ${rule}`); continue; }
    const [, level, fgs, bgs] = m;
    for (const bgSpec of bgs.split(/\s+/)) {
      for (const fgSpec of fgs.split(/\s+/)) {
        covered.add(fgSpec.split('*')[0]);
        try {
          const bg = background(tokens, bgSpec);
          const fg = foreground(tokens, fgSpec, bg);
          const value = ratio(fg, bg);
          results.push({ level, fg: fgSpec, bg: bgSpec, fgHex: hex(fg), bgHex: hex(bg), ratio: value, min: THRESHOLDS[level], ok: value >= THRESHOLDS[level] });
        } catch (error) {
          errors.push(`${fgSpec} / ${bgSpec}: ${error.message}`);
        }
      }
    }
  }
  for (const name of tokens.keys()) {
    if (FOREGROUND.test(name) && !covered.has(name)) errors.push(`${name}: цвет текста без пары @contrast`);
  }
  return { tokens: tokens.size, results, errors, ok: !errors.length && results.every((r) => r.ok) };
}

function main(argv) {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const file = argv.find((a) => !a.startsWith('--')) || path.join(here, '..', 'project', 'tokens.css');
  const report = check(readFileSync(file, 'utf8'));
  if (argv.includes('--json')) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    const failed = report.results.filter((r) => !r.ok);
    const worst = [...report.results].sort((a, b) => a.ratio / a.min - b.ratio / b.min).slice(0, 8);
    console.log(`Токенов: ${report.tokens}; пар: ${report.results.length}; ниже порога: ${failed.length}; ошибок разбора: ${report.errors.length}`);
    for (const r of failed) console.log(`  FAIL ${r.level.padEnd(5)} ${r.ratio.toFixed(2)} < ${r.min}  ${r.fg} (${r.fgHex}) на ${r.bg} (${r.bgHex})`);
    for (const e of report.errors) console.log(`  ERROR ${e}`);
    console.log('Ближайшие к порогу:');
    for (const r of worst) console.log(`  ${r.ok ? 'ok  ' : 'FAIL'} ${r.level.padEnd(5)} ${r.ratio.toFixed(2)} (порог ${r.min})  ${r.fg} на ${r.bg}`);
    console.log(report.ok ? 'Контраст: OK' : 'Контраст: ПРОВАЛ');
  }
  return report.ok ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  process.exitCode = main(process.argv.slice(2));
}
