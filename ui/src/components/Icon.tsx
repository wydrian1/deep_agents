import type { CSSProperties } from 'react';

export type IconName =
  | 'graph' | 'spark' | 'play' | 'pause' | 'clock' | 'calendar' | 'check' | 'close' | 'plus' | 'minus' | 'fit' | 'search'
  | 'chevron' | 'file' | 'upload' | 'globe' | 'github' | 'terminal' | 'shield' | 'layers' | 'activity' | 'arrow' | 'download'
  | 'more' | 'refresh' | 'trash' | 'save' | 'message' | 'bell' | 'sun' | 'moon' | 'logout';

const paths: Record<IconName, string> = {
  bell: 'M5 17h14l-2-3V9a5 5 0 0 0-10 0v5zM10 21h4',
  graph: 'M12 3v5M5 16v-4h14v4M12 8v4M9 3h6v5H9zM2 16h6v5H2zM16 16h6v5h-6z',
  spark: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z',
  play: 'm8 5 11 7-11 7z',
  pause: 'M8 5v14M16 5v14',
  clock: 'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  calendar: 'M4 5h16v16H4zM4 10h16M8 3v4M16 3v4M8 14h2M14 14h2M8 17h2',
  check: 'm5 12 4 4L19 6',
  close: 'm6 6 12 12M18 6 6 18',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  fit: 'M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  chevron: 'm9 5 7 7-7 7',
  file: 'M5 3h9l5 5v13H5zM14 3v6h5M8 13h8M8 17h6',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',
  globe: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18',
  github: 'M9 19c-4 1-4-2-6-2M15 22v-4c0-1-.4-2-1-2 4-.5 6-2 6-6 0-1-.3-2-1-3 .3-1 .3-3-.2-4-2 0-3 1-4 1-2-.5-4-.5-6 0-1 0-2-1-4-1-.5 1-.5 3-.2 4-.7 1-1 2-1 3 0 4 2 5.5 6 6-.6 0-1 1-1 2v4',
  terminal: 'm5 7 5 5-5 5M13 17h6',
  shield: 'm12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6',
  layers: 'm12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5',
  activity: 'M2 12h5l3-8 4 16 3-8h5',
  arrow: 'M4 12h16m-6-6 6 6-6 6',
  download: 'M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4',
  more: 'M5 11v2M12 11v2M19 11v2',
  refresh: 'M20 7v5h-5M4 17v-5h5M19 8a8 8 0 0 0-14-2M5 16a8 8 0 0 0 14 2',
  trash: 'M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7',
  save: 'M4 3h13l4 4v14H3V3h1M7 3v6h10V3M7 21v-8h10v8',
  message: 'M4 4h16v12H9l-5 5zM8 8h8M8 12h5',
  sun: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  moon: 'M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z',
  logout: 'M9 4H4v16h5M16 8l4 4-4 4M20 12H9',
};

export default function Icon({ name, size = 18, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" style={style}>
      <path d={paths[name]} />
    </svg>
  );
}
