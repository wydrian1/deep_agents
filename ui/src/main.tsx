import { StrictMode, useCallback, useState } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n';
import './styles/app.css';
import './styles/agents.css';
import './styles/agents-light.css';
import './styles/run-stats.css';
import './styles/schedule.css';
import './styles/extra.css';
import './styles/workflows.css';

function Root() {
  // The service's APP_LANGUAGE is the default until the user picks a language themselves.
  const [serverLanguage, setServerLanguage] = useState<string | undefined>();
  const onLanguage = useCallback((language: string | undefined) => setServerLanguage((current) => current ?? language), []);
  return (
    <I18nProvider serverLanguage={serverLanguage}>
      <App onLanguage={onLanguage} />
    </I18nProvider>
  );
}

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
