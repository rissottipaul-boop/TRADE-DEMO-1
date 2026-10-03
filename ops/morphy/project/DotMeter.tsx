// Точечный счётчик «входов за день»: ●●●○○○○○○○ 3 / 10. Цвет — по тем же порогам, что ThresholdBar.
// Лимит больше MAX_DOTS — точки нечитаемы, рисуется ThresholdBar.
import { useId } from 'react';
import type { ReactNode } from 'react';
import { fmtInt, isNum, thresholdTone } from './format';
import ThresholdBar from './ThresholdBar';
import './tokens.css';
import './dot-meter.css';

export const MAX_DOTS = 20;

export interface DotMeterProps {
  value: number | null | undefined;
  max: number | null | undefined;
  /** Подпись: «Входов за день». */
  label: string;
  warnAt?: number;
  critAt?: number;
  hideLabel?: boolean;
  hint?: ReactNode;
  className?: string;
}

export default function DotMeter({ value, max, label, warnAt = 0.6, critAt = 0.85, hideLabel = false, hint, className }: DotMeterProps) {
  const labelId = useId();
  const hasMax = isNum(max) && max > 0 && Number.isInteger(max);
  if (hasMax && max > MAX_DOTS) {
    return <ThresholdBar value={value} limit={max} label={label} format={fmtInt} warnAt={warnAt} critAt={critAt} hideLabel={hideLabel} hint={hint} className={className} />;
  }
  const hasValue = isNum(value);
  const tone = thresholdTone(value, max, warnAt, critAt);
  const filled = hasValue && hasMax ? Math.min(max, Math.max(0, Math.round(value))) : 0;
  const over = hasValue && hasMax ? Math.max(0, Math.round(value) - max) : 0;
  const shown = hasValue ? (hasMax ? `${fmtInt(value)} / ${fmtInt(max)}` : fmtInt(value)) : 'нет данных';
  const spoken = hasValue ? (hasMax ? `${fmtInt(value)} из ${fmtInt(max)}` : fmtInt(value)) : 'нет данных';
  const a11y = hasValue && hasMax
    ? { role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': max, 'aria-valuenow': value, 'aria-valuetext': spoken }
    : { role: 'img' };
  const classes = ['pj-dots', className].filter(Boolean).join(' ');
  return (
    <div className={classes} data-tone={tone}>
      {!hideLabel && (
        <div className="pj-dots-head">
          <span className="pj-dots-label" id={labelId}>{label}{hint}</span>
          <span className="pj-dots-value pj-num">{shown}</span>
        </div>
      )}
      <div
        className="pj-dots-row"
        {...a11y}
        aria-labelledby={!hideLabel && hasValue && hasMax ? labelId : undefined}
        aria-label={hideLabel || !(hasValue && hasMax) ? `${label}: ${spoken}` : undefined}
      >
        {hasMax
          ? Array.from({ length: max }, (_, i) => <span key={i} className={i < filled ? 'pj-dot-on' : 'pj-dot-off'} aria-hidden="true" />)
          : <span className="pj-dots-none" aria-hidden="true">нет лимита</span>}
        {over > 0 && <span className="pj-dots-over pj-num" aria-hidden="true">+{over}</span>}
      </div>
    </div>
  );
}
