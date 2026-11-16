import type { ToolInfo } from '../types';
import type { IconName } from '../components/Icon';
import type { T } from './schedule';

// Presentation of the tools offered by the backend catalog (GET /api/tools). Unknown tools get a generic icon.
const ICONS: Record<string, IconName> = {
  webfetch: 'globe', websearch: 'search', github: 'github', files: 'file', file_search: 'search', document: 'file', report: 'save',
};
export const toolIcon = (name: string): IconName => ICONS[name] ?? 'layers';

/** Select/deselect a tool together with its dependencies (selecting adds what it requires, deselecting removes dependants). */
export function toggleTool(current: string[], id: string, catalog: ToolInfo[]): string[] {
  const requires = (name: string) => catalog.find((tool) => tool.name === name)?.requires ?? [];
  const next = new Set(current);
  if (next.has(id)) {
    next.delete(id);
    let changed = true;
    while (changed) {
      changed = false;
      for (const tool of [...next]) {
        if (requires(tool).some((required) => !next.has(required))) {
          next.delete(tool);
          changed = true;
        }
      }
    }
  } else {
    const add = (name: string) => {
      if (next.has(name)) return;
      for (const required of requires(name)) add(required);
      next.add(name);
    };
    add(id);
  }
  return [...next];
}

/** Localized tool name/description; falls back to the backend catalog text for tools the UI does not know. */
export function toolText(tool: ToolInfo, field: 'label' | 'description', t: T): string {
  const key = `tools.${tool.name}.${field}`;
  const value = t(key);
  return value === key ? tool[field] : value;
}
export const toolLabelById = (name: string, catalog: ToolInfo[], t: T) => {
  const tool = catalog.find((item) => item.name === name);
  return tool ? toolText(tool, 'label', t) : name;
};
