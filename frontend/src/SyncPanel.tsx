import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { RUN_LABELS, SYNC_ERROR_LABELS, enqueueSync, getSyncConfiguration, saveSyncConfiguration, syncDraft, validateSyncDraft } from './sync'
import InitialImportPanel from './InitialImportPanel'
import type { SyncConfiguration, SyncDraft, SyncSettings } from './sync'

function message(error: unknown) { return error instanceof Error ? error.message : 'Synchronisation indisponible.' }
function dateLabel(value: string | null) { return value ? new Date(value).toLocaleString('fr-CA') : 'Aucune' }

export function SyncForm({ config, busy, onSave }: { config: SyncSettings; busy: boolean; onSave: (draft: SyncDraft) => Promise<void> }) {
  const [draft, setDraft] = useState(() => syncDraft(config))
  const [error, setError] = useState('')
  function update<K extends keyof SyncDraft>(key: K, value: SyncDraft[K]) {
    setDraft(current => ({ ...current, [key]: value, ...(key === 'auto_analysis_enabled' ? { consent: false } : {}) }))
    setError('')
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const validation = validateSyncDraft(config, draft)
    setError(validation)
    if (!validation) void onSave(draft)
  }
  return <form onSubmit={submit} aria-label="Paramètres de synchronisation" aria-busy={busy}>
    {error && <p role="alert" className="error">{error}</p>}
    <fieldset className="form-grid" disabled={busy}>
      <legend className="sr-only">Planification et consentement</legend>
      <label className="check-field full-width"><input type="checkbox" checked={draft.gmail_enabled}
        onChange={event => update('gmail_enabled', event.target.checked)} /> Activer la synchronisation Gmail toutes les cinq minutes</label>
      <div className="field full-width"><label htmlFor="sync-zone">Fuseau horaire</label><input id="sync-zone" required maxLength={64} value={draft.timezone} onChange={event => update('timezone', event.target.value)} /></div>
      <label className="check-field full-width"><input type="checkbox" checked={draft.auto_analysis_enabled}
        onChange={event => update('auto_analysis_enabled', event.target.checked)} /> Analyser automatiquement les nouveaux messages avec Gemini</label>
      <p className="full-width sync-note">Désactivée par défaut, l’analyse transmet le sujet et le texte des nouveaux messages à Google. Elle ne concerne pas les messages antérieurs au consentement et n’applique jamais de proposition. Pour les essais avec l’offre gratuite, utilisez uniquement des courriels fictifs.</p>
      {draft.auto_analysis_enabled && !config.auto_analysis_enabled && <label className="check-field full-width">
        <input type="checkbox" checked={draft.consent} onChange={event => update('consent', event.target.checked)} />
        Je consens explicitement à cette transmission automatique des nouveaux messages à Gemini.
      </label>}
      {config.auto_analysis_enabled && <p className="full-width sync-note">Consentement enregistré le {dateLabel(config.consent_at)}. Pour le révoquer, décochez l’analyse automatique puis enregistrez. Un appel déjà engagé ne peut pas être retiré ; sa réponse sera écartée si le consentement a été révoqué avant son enregistrement.</p>}
    </fieldset>
    <div className="actions"><button type="submit" className="secondary" disabled={busy}>Enregistrer les paramètres</button></div>
  </form>
}

export default function SyncPanel({ disabled, onPendingChange, onUpdated }: {
  disabled: boolean; onPendingChange: (pending: boolean) => void; onUpdated: () => void
}) {
  const [config, setConfig] = useState<SyncConfiguration | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const mutation = useRef(false)
  const previous = useRef<SyncSettings | null>(null)
  const updatedCallback = useRef(onUpdated)
  useEffect(() => { updatedCallback.current = onUpdated }, [onUpdated])
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function read() {
      try {
        const data = await getSyncConfiguration(controller.signal)
        if (controller.signal.aborted) return
        if (data.connected) {
          const old = previous.current
          if (old && (old.analyses_completed !== data.analyses_completed || data.latest_run?.status === 'completed' &&
            (old.latest_run?.id !== data.latest_run.id || old.latest_run?.status !== 'completed'))) updatedCallback.current()
          previous.current = data
        }
        setConfig(data)
        setError('')
      } catch (cause) {
        if (!controller.signal.aborted) setError(message(cause))
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false)
          // Read-only status polling; processing never depends on this timer.
          timer = setTimeout(() => void read(), 15000)
        }
      }
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [reload])

  async function act(draft?: SyncDraft) {
    if (disabled || loading || busy || mutation.current || !config?.connected) return
    mutation.current = true
    setBusy(true); onPendingChange(true); setError(''); setNotice('')
    try {
      if (draft) {
        setConfig(await saveSyncConfiguration(config, draft))
        setNotice('Paramètres enregistrés. Aucune proposition ne sera appliquée automatiquement.')
      } else {
        const run = await enqueueSync()
        setConfig(current => current?.connected ? { ...current, latest_run: run } : current)
        setNotice('Synchronisation enregistrée. Le serveur la poursuivra même si vous fermez cette page.')
      }
      setReload(value => value + 1)
    } catch (cause) { setError(message(cause)) }
    finally { mutation.current = false; setBusy(false); onPendingChange(false) }
  }
  const active = config?.connected && config.latest_run && ['queued','running','paused'].includes(config.latest_run.status)
  const locked = disabled || loading || busy
  return <section className="sync-panel" aria-labelledby="sync-heading" aria-busy={busy || loading}>
    <div className="list-toolbar"><h2 id="sync-heading">Synchronisation Gmail</h2>
      <button type="button" className="secondary" disabled={locked} onClick={() => { setLoading(true); setReload(value => value + 1) }}>Actualiser le suivi</button>
      <button type="button" disabled={locked || !config?.connected || Boolean(active) || config.connected && Boolean(config.blocked_code)} onClick={() => void act()}>Synchroniser maintenant</button>
    </div>
    <p>Messages reçus, envoyés et archivés, sans filtrage par mots-clés. Spams, corbeille et brouillons exclus. La première synchronisation couvre les dernières 24 heures.</p>
    <p className="sync-note">Un serveur permanent doit exécuter la commande planifiée. Fermer le navigateur n’interrompt pas les travaux ; un ordinateur local éteint ne peut pas les exécuter.</p>
    {notice && <p className="success" role="status">{notice}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {loading && <p role="status">Chargement du suivi…</p>}
    {config && !config.connected && <p>Connectez d’abord votre compte Gmail.</p>}
    {config?.connected && <>
      {config.blocked_code && <p className="error" role="alert">{SYNC_ERROR_LABELS[config.blocked_code] ?? 'Vérifiez la connexion Gmail avant de reprendre.'}</p>}
      <dl className="proposal-facts">
        <div><dt>Dernière synchronisation réussie</dt><dd>{dateLabel(config.last_success_at)}</dd></div>
        <div><dt>Prochaine échéance</dt><dd>{dateLabel(config.next_due_at)}</dd></div>
        <div><dt>Analyses en attente ou en cours</dt><dd>{config.analysis_pending}</dd></div>
        <div><dt>Analyses en échec définitif</dt><dd>{config.analyses_failed}</dd></div>
      </dl>
      {config.last_analysis_error && <p className="proposal-warning">{SYNC_ERROR_LABELS[config.last_analysis_error] ?? 'Une analyse a rencontré une erreur. Vérifiez le suivi avant toute relance.'}</p>}
      {config.latest_run && config.latest_run.origin !== 'initial' && <div className="sync-progress" role="status">
        <p>Dernier travail : {RUN_LABELS[config.latest_run.status]}</p>
        <p>{config.latest_run.processed} examinés · {config.latest_run.imported} importés · {config.latest_run.duplicates} doublons évités · {config.latest_run.skipped} ignorés</p>
        {config.latest_run.error_code && <p>{SYNC_ERROR_LABELS[config.latest_run.error_code] ?? 'Une erreur a interrompu le travail. Vérifiez le suivi.'}</p>}
        {config.latest_run.retry_after && <p>Reprise possible à partir de {dateLabel(config.latest_run.retry_after)}.</p>}
      </div>}
      <InitialImportPanel job={config.latest_initial_import} disabled={locked || Boolean(active) || Boolean(config.blocked_code)}
        onPendingChange={value => { setBusy(value); onPendingChange(value) }} onQueued={run => {
          setConfig(current => current?.connected ? { ...current, latest_initial_import: run, latest_run: run } : current)
          setReload(value => value + 1)
        }} />
      <SyncForm key={config.revision} config={config} busy={locked} onSave={draft => act(draft)} />
    </>}
  </section>
}
