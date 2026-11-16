import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { translate } from './index';
import { en } from './en';
import { hu } from './hu';

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
function sources(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.|[\\/]i18n[\\/](en|hu)\.ts$/.test(path) ? [path] : [];
  });
}

describe('dictionaries', () => {
  it('have exactly the same keys, all non-empty', () => {
    expect(Object.keys(hu).sort()).toEqual(Object.keys(en).sort());
    for (const [key, value] of Object.entries({ ...en })) expect(value.trim(), key).not.toBe('');
    for (const [key, value] of Object.entries(hu)) expect(value.trim(), key).not.toBe('');
  });

  it('use the same {placeholders} in both languages', () => {
    for (const key of Object.keys(en)) expect(placeholders(hu[key]), key).toEqual(placeholders(en[key]));
  });

  it('define every literal key the source uses, plus the dynamic families', () => {
    const used = new Set<string>();
    for (const file of sources(join(__dirname, '..'))) {
      for (const match of readFileSync(file, 'utf8').matchAll(/\b(?:t|tt)\(\s*'([A-Za-z0-9_.]+)'/g)) used.add(match[1]);
    }
    const dynamic = [
      ...[0, 1, 2, 3, 4, 5, 6].flatMap((day) => ['weekday.' + day, 'weekdayShort.' + day]),
      ...['unknown', 'success', 'noop', 'blocked', 'error'].map((v) => 'outcome.' + v),
      ...['__start__', '__end__', 'model', 'tools', 'patchTools', 'todos', 'close'].map((v) => 'node.' + v),
      ...['startup', 'model', 'tool', 'other'].map((v) => 'stats.share.' + v),
      ...['running', 'completed', 'failed', 'cancelled'].map((v) => 'run.status.' + v),
      ...['research', 'repo', 'documents'].flatMap((id) => ['name', 'summary', 'prompt'].map((f) => `templates.${id}.${f}`)),
      'tool.readOnly', 'tool.privateWrite',
      ...['webfetch', 'websearch', 'github', 'files', 'file_search', 'document', 'report'].flatMap((id) => ['label', 'description'].map((f) => `tools.${id}.${f}`)),
    ];
    const prefixes = ['node.', 'outcome.', 'run.status.', 'stats.share.', 'weekday.', 'weekdayShort.'];
    const missing = [...used, ...dynamic].filter((key) => !prefixes.includes(key) && !(key in en));
    expect(missing).toEqual([]);
  });
});

describe('translate', () => {
  it('fills values and falls back to English, then to the key', () => {
    expect(translate('hu', 'library.next', { time: '08:30' })).toBe('Következő: 08:30');
    expect(translate('en', 'library.next', { time: '08:30' })).toBe('Next: 08:30');
    expect(translate('en', 'no.such.key')).toBe('no.such.key');
  });
});
