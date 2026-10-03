// Спарклайн ~40 px под значением карточки: лёгкий SVG без recharts.
// Серия [[ts, v], …] (ось X — время) или массив чисел (ось X — индекс).
import { fmtNumber, isNum } from './format';
import type { Series } from './types';
import './tokens.css';
import './spark.css';

export type SparkTone = 'neutral' | 'accent' | 'pos' | 'neg' | 'warn' | 'crit' | 'auto';

export interface SparkProps {
  data: Series | readonly number[] | null | undefined;
  /** Что показывает линия: «Капитал за 24 ч». Идёт в aria-label с мин/макс/последним. */
  label: string;
  /** auto — последнее ≥ первого: pos, иначе neg. По умолчанию neutral. */
  tone?: SparkTone;
  /** Высота в px (по умолчанию 40). Ширина — 100% контейнера. */
  height?: number;
  /** Пунктирная опорная линия (0 для PnL); входит в диапазон оси Y. */
  baseline?: number | null;
  /** Заливка под линией (по умолчанию да). */
  area?: boolean;
  /** Меньше двух точек: hide — ничего не рисовать, stub — пунктирная заглушка «нет истории». */
  empty?: 'hide' | 'stub';
  /** Формат значений для aria-описания. */
  format?: (value: number) => string;
  className?: string;
}

const VIEW_W = 100;
const PAD = 3;

interface XY { x: number; y: number }

/** Точки серии в координатах viewBox (чистая функция, экспорт для тестов/графиков). */
export function sparkGeometry(values: Array<[number, number]>, height: number, baseline: number | null = null): { points: XY[]; baseY: number | null; min: number; max: number } {
  const ys = values.map((p) => p[1]);
  let min = Math.min(...ys);
  let max = Math.max(...ys);
  if (isNum(baseline)) { min = Math.min(min, baseline); max = Math.max(max, baseline); }
  const t0 = values[0][0];
  const t1 = values[values.length - 1][0];
  const spanX = t1 - t0 || 1;
  const spanY = max - min;
  const inner = Math.max(1, height - PAD * 2);
  const toY = (v: number) => (spanY === 0 ? height / 2 : PAD + (1 - (v - min) / spanY) * inner);
  const points = values.map(([t, v]) => ({ x: ((t - t0) / spanX) * VIEW_W, y: toY(v) }));
  return { points, baseY: isNum(baseline) ? toY(baseline) : null, min, max };
}

function normalize(data: SparkProps['data']): Array<[number, number]> {
  if (!Array.isArray(data)) return [];
  const out: Array<[number, number]> = [];
  data.forEach((item: unknown, index: number) => {
    if (Array.isArray(item)) {
      if (isNum(item[0]) && isNum(item[1])) out.push([item[0], item[1]]);
    } else if (isNum(item)) {
      out.push([index, item]);
    }
  });
  return out;
}

export default function Spark({ data, label, tone = 'neutral', height = 40, baseline = null, area = true, empty = 'hide', format, className }: SparkProps) {
  const values = normalize(data);
  const fmt = format ?? ((v: number) => fmtNumber(v, 2));
  const classes = ['pj-spark', className].filter(Boolean).join(' ');
  if (values.length < 2) {
    if (empty === 'hide') return null;
    return (
      <div className={classes + ' pj-spark--empty'} style={{ height }} role="img" aria-label={`${label}: нет истории`}>
        <svg viewBox={`0 0 ${VIEW_W} ${height}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
          <line x1="0" x2={VIEW_W} y1={height / 2} y2={height / 2} vectorEffect="non-scaling-stroke" />
        </svg>
      </div>
    );
  }
  const first = values[0][1];
  const last = values[values.length - 1][1];
  const resolved = tone === 'auto' ? (last >= first ? 'pos' : 'neg') : tone;
  const { points, baseY, min, max } = sparkGeometry(values, height, baseline);
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(' ');
  const fill = `${line} L${VIEW_W},${height} L0,${height} Z`;
  const end = points[points.length - 1];
  const description = `${label}: от ${fmt(min)} до ${fmt(max)}, последнее ${fmt(last)}`;
  return (
    <div className={classes} data-tone={resolved} style={{ height }} role="img" aria-label={description}>
      <svg viewBox={`0 0 ${VIEW_W} ${height}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
        {area && <path className="pj-spark-area" d={fill} />}
        {baseY != null && <line className="pj-spark-base" x1="0" x2={VIEW_W} y1={baseY} y2={baseY} vectorEffect="non-scaling-stroke" />}
        <path className="pj-spark-line" d={line} vectorEffect="non-scaling-stroke" />
      </svg>
      <span className="pj-spark-dot" aria-hidden="true" style={{ left: `${end.x}%`, top: `${end.y}px` }} />
    </div>
  );
}
