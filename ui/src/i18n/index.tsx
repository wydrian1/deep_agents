import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { en } from './en';
import { hu } from './hu';

export type Lang = 'en' | 'hu';
export const LANGUAGES: Lang[] = ['en', 'hu'];
const dictionaries: Record<Lang, Record<string, string>> = { en, hu };
const STORAGE_KEY = 'deep-agents-lang';

export function translate(lang: Lang, key: string, values?: Record<string, string | number>): string {
  const template = dictionaries[lang][key] ?? en[key] ?? key;
  return values ? template.replace(/\{(\w+)\}/g, (_, name: string) => String(values[name] ?? '')) : template;
}

const stored = (): Lang | null => {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === 'en' || value === 'hu' ? value : null;
  } catch {
    return null;
  }
};
const fromBrowser = (): Lang => (typeof navigator !== 'undefined' && navigator.language?.toLowerCase().startsWith('hu') ? 'hu' : 'en');
const asLang = (value: string | undefined): Lang | null => (value === 'en' || value === 'hu' ? value : null);

type Ctx = { lang: Lang; setLang: (lang: Lang) => void; t: (key: string, values?: Record<string, string | number>) => string };
const I18nContext = createContext<Ctx>({ lang: 'en', setLang: () => undefined, t: (key, values) => translate('en', key, values) });

/** The user's own choice wins; otherwise the service's APP_LANGUAGE; otherwise the browser language. */
export function I18nProvider({ serverLanguage, children }: { serverLanguage?: string; children: ReactNode }) {
  const [choice, setChoice] = useState<Lang | null>(stored);
  const lang = choice ?? asLang(serverLanguage) ?? fromBrowser();
  const setLang = useCallback((next: Lang) => {
    setChoice(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* private mode: the choice just lasts for this page view */
    }
  }, []);
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);
  const value = useMemo<Ctx>(() => ({ lang, setLang, t: (key, values) => translate(lang, key, values) }), [lang, setLang]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export const useI18n = () => useContext(I18nContext);
