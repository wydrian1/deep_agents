import { artifactUrl } from '../api';
import { useI18n } from '../i18n';
import { kilobytes } from '../lib/format';
import type { Run } from '../types';
import Icon from './Icon';

/** Private run outputs (reports). Downloading is the only action: nothing is published or shared. */
export default function RunArtifacts({ run }: { run: Run }) {
  const { t } = useI18n();
  if (!run.artifacts?.length) return null;
  return (
    <section className="studio-artifacts" aria-label={t('artifacts.label')}>
      <div>
        <h3><Icon name="file" size={17} />{t('artifacts.title')} <span>{run.artifacts.length}</span></h3>
        <p>{t('artifacts.hint')}</p>
      </div>
      {run.artifacts.map((item) => (
        <article key={item.id}>
          <div><strong>{item.title}</strong><small>{item.name} · {kilobytes(item.size)} KB</small></div>
          <div>
            <a className="studio-button" href={artifactUrl(run.id, item.id)} download={item.name}><Icon name="download" size={14} />{t('artifacts.download')}</a>
          </div>
        </article>
      ))}
    </section>
  );
}
