// Пороговая полоса value/limit: < 60 % лимита — зелёный, 60–85 % — амбер, > 85 % — красный,
// засечка на лимите. Нет значения — пунктирная дорожка и «нет данных», не пустой зелёный бар.
import { useId } from 'react';
import type { ReactNode } from 'react';
import { fmtNumber, isNum, thresholdTone } from './format';
import './tokens.css';
import './threshold-bar.css';

export interface ThresholdBarProps {
  value: number | null | undefined;
  limit: number | null | undefined;
  /** Подпись: «Риск позиций». */
  label: string;
  /** Единица после чисел: «%». */
  unit?: string;
  /** Формат чисел (по умолчанию до 2 знаков). */
  format?: (value: number) => string;
  /** Доли лимита для амбера и красного (по умолчанию 0.6 и 0.85). */
  warnAt?: number;
  critAt?: number;
  /** Шкала до limit × headroom, чтобы засечка лимита и превышение были видны (по умолчанию 1.15). */
  headroom?: number;
  /** Скрыть строку подписи (подпись остаётся в aria-label). */
  hideLabel?: boolean;
  /** Элемент рядом с подписью, например <InfoTip>. */
  hint?: ReactNode;
  className?: string;
}

const clamp = (n: number) => Math.min(100, Math.max(0, n));

export default function ThresholdBar({ value, limit, label, unit = '', format, warnAt = 0.6, critAt = 0.85, headroom = 1.15, hideLabel = false, hint, className }: ThresholdBarProps) {
  const labelId = useId();
  const fmt = format ?? ((v: number) => fmtNumber(v, 2));
  const hasValue = isNum(value);
  const hasLimit = isNum(limit) && limit > 0;
  const tone = thresholdTone(value, limit, warnAt, critAt);
  const scale = hasLimit ? limit * Math.max(1, headroom) : hasValue ? Math.max(Math.abs(value), 1) : 1;
  const fill = hasValue ? clamp((Math.max(0, value) / scale) * 100) : 0;
  const mark = hasLimit ? clamp((limit / scale) * 100) : null;
  const share = hasValue && hasLimit ? Math.round((value / limit) * 100) : null;
  const valueText = hasValue ? fmt(value) : 'нет данных';
  const limitText = hasLimit ? fmt(limit) + unit : null;
  const shown = hasValue ? (limitText ? `${valueText} / ${limitText}` : valueText + unit) : 'нет данных';
  const spoken = hasValue
    ? `${valueText}${unit} из ${limitText ?? 'неизвестного лимита'}${share != null ? `, ${share}% лимита` : ''}`
    : 'нет данных';
  const classes = ['pj-tbar', className].filter(Boolean).join(' ');
  // role=meter требует aria-valuenow; без значения — изображение с текстом «нет данных».
  const a11y = hasValue
    ? { role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': hasLimit ? limit : undefined, 'aria-valuenow': value, 'aria-valuetext': spoken }
    : { role: 'img' };
  return (
    <div className={classes} data-tone={tone}>
      {!hideLabel && (
        <div className="pj-tbar-head">
          <span className="pj-tbar-label" id={labelId}>{label}{hint}</span>
          <span className="pj-tbar-value pj-num">{shown}</span>
        </div>
      )}
      <div
        className={'pj-tbar-track' + (hasValue ? '' : ' pj-tbar-track--empty')}
        {...a11y}
        aria-labelledby={hideLabel || !hasValue ? undefined : labelId}
        aria-label={!hasValue ? `${label}: нет данных` : hideLabel ? label : undefined}
      >
        {hasValue && <span className="pj-tbar-fill" style={{ width: `${fill}%` }} />}
        {mark != null && <span className="pj-tbar-limit" style={{ left: `${mark}%` }} aria-hidden="true" />}
      </div>
    </div>
  );
}
