// Скелетоны вместо «Загружаю данные проекта…»: каркас той же геометрии, что и контент,
// чтобы при приходе данных экран не прыгал. Мерцание отключается при prefers-reduced-motion.
import type { CSSProperties, ReactNode } from 'react';
import './tokens.css';
import './skeleton.css';

export interface SkeletonProps {
  width?: number | string;
  height?: number | string;
  radius?: number | string;
  className?: string;
  style?: CSSProperties;
}

/** Один серый брусок. Сам по себе скрыт от скринридеров — статус объявляет SkeletonGroup. */
export default function Skeleton({ width = '100%', height = 12, radius, className, style }: SkeletonProps) {
  return (
    <span
      className={['pj-skel', className].filter(Boolean).join(' ')}
      aria-hidden="true"
      style={{ width, height, borderRadius: radius, ...style }}
    />
  );
}

/** Несколько строк текста; последняя короче. */
export function SkeletonText({ lines = 3, lineHeight = 11, gap = 9, lastWidth = '62%' }: { lines?: number; lineHeight?: number; gap?: number; lastWidth?: string }) {
  return (
    <span className="pj-skel-text" aria-hidden="true" style={{ gap }}>
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} height={lineHeight} width={i === lines - 1 && lines > 1 ? lastWidth : '100%'} />
      ))}
    </span>
  );
}

/** Каркас карточки метрики: подпись, значение, спарклайн, подсказка. */
export function SkeletonCard({ spark = true }: { spark?: boolean }) {
  return (
    <div className="pj-skel-card" aria-hidden="true">
      <Skeleton width="45%" height={11} />
      <Skeleton width="70%" height={22} radius={6} />
      {spark && <Skeleton height={40} radius={6} />}
      <Skeleton width="55%" height={10} />
    </div>
  );
}

/** Каркас панели: заголовок и строки. */
export function SkeletonPanel({ rows = 4, chart = false }: { rows?: number; chart?: boolean }) {
  return (
    <div className="pj-skel-panel" aria-hidden="true">
      <Skeleton width="34%" height={15} />
      <Skeleton width="52%" height={11} />
      {chart ? <Skeleton height={220} radius={9} /> : <SkeletonText lines={rows} />}
    </div>
  );
}

/**
 * Обёртка загрузки: объявляет скринридеру «Загружаю…» (role=status, aria-busy),
 * визуально показывает каркас из children.
 */
export function SkeletonGroup({ label = 'Загружаю данные проекта…', children, className }: { label?: string; children: ReactNode; className?: string }) {
  return (
    <div className={['pj-skel-group', className].filter(Boolean).join(' ')} role="status" aria-live="polite" aria-busy="true">
      <span className="pj-sr-only">{label}</span>
      {children}
    </div>
  );
}
