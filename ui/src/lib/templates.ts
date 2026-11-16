// Starting points for the composer. A template only pre-fills the form: it never saves, schedules or starts
// anything, and every consent stays an explicit choice. Texts live in the i18n dictionaries.
export type AgentTemplate = { id: string; tools: string[] };
export const AGENT_TEMPLATES: AgentTemplate[] = [
  { id: 'research', tools: ['websearch', 'webfetch', 'report'] },
  { id: 'repo', tools: ['github', 'report'] },
  { id: 'documents', tools: ['files', 'document', 'file_search', 'report'] },
];
