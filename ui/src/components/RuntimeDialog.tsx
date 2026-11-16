import { forwardRef } from 'react';
import { useI18n } from '../i18n';
import { formatDate } from '../lib/format';
import type { ServiceStatus } from '../types';
import Icon from './Icon';

type Props = { status: ServiceStatus | null; schedulerReady: boolean; account: string };

/** Connection and security overview: what this installation is configured to do (never any secret). */
const RuntimeDialog = forwardRef<HTMLDialogElement, Props>(function RuntimeDialog({ status, schedulerReady, account }, ref) {
  const { t, lang } = useI18n();
  const zone = status?.schedule_timezone ?? 'UTC';
  const close = () => (ref as React.RefObject<HTMLDialogElement>).current?.close();
  return (
    <dialog ref={ref} className="studio-dialog studio-detail-dialog" aria-labelledby="studio-runtime-title">
      <div className="studio-dialog-heading">
        <div><span className="studio-kicker">{t('runtime.kicker')}</span><h2 id="studio-runtime-title">{t('runtime.title')}</h2></div>
        <button className="studio-icon-button" onClick={close} aria-label={t('common.close')}><Icon name="close" /></button>
      </div>
      <div className="studio-dialog-body">
        <dl className="studio-facts">
          <div><dt>{t('runtime.backend')}</dt><dd>{status ? t('runtime.available') : t('runtime.unavailable')}</dd></div>
          <div><dt>{t('runtime.model')}</dt><dd>{status?.model || t('runtime.noDefaultModel')}</dd></div>
          <div><dt>{t('runtime.scheduler')}</dt><dd>{!status?.scheduler_enabled ? t('runtime.schedulerOff') : schedulerReady ? t('runtime.schedulerActive') : t('runtime.noHeartbeat')}</dd></div>
          <div><dt>{t('runtime.timezone')}</dt><dd>{zone}</dd></div>
          <div><dt>{t('runtime.limits')}</dt><dd>{t('runtime.limitsValue', { max: status?.max_concurrent_runs ?? 3, minutes: Math.round((status?.run_timeout_seconds ?? 3600) / 60) })}</dd></div>
          <div><dt>{t('runtime.auth')}</dt><dd>{status?.auth_enabled ? account || t('runtime.authOn') : t('runtime.authOff')}</dd></div>
          {status?.limit_paused_until && <div><dt>{t('runtime.pausedUntil')}</dt><dd>{formatDate(status.limit_paused_until, zone, lang)}</dd></div>}
        </dl>
        <h3>{t('runtime.gatewayTitle')}</h3>
        <p>{t('runtime.gatewayBody')}</p>
        {!status?.auth_enabled && <div className="studio-inspector-note"><Icon name="shield" /><p>{t('runtime.noLoginWarning')}</p></div>}
      </div>
    </dialog>
  );
});
export default RuntimeDialog;
