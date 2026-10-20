import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { enqueueInitialImport, RUN_LABELS, SYNC_ERROR_LABELS, validateImportPeriod } from './sync'
import type { ImportPeriod, SyncRun } from './sync'

export function InitialImportForm({ period, busy, onChange, onSubmit }: {
  period: ImportPeriod; busy: boolean; onChange: (period: ImportPeriod) => void; onSubmit: () => void
}) {
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!busy) onSubmit() }
  return <form onSubmit={submit} aria-label="Import initial Gmail" aria-busy={busy}>
    <fieldset className="form-grid" disabled={busy}>
      <legend>Importer une période</legend>
      <div className="field"><label htmlFor="import-start">Date de début incluse</label>
        <input id="import-start" name="date_debut" type="date" required value={period.date_debut}
          onChange={event => onChange({ ...period, date_debut: event.target.value })} /></div>
      <div className="field"><label htmlFor="import-end">Date de fin incluse</label>
        <input id="import-end" name="date_fin" type="date" required value={period.date_fin}
          onChange={event => onChange({ ...period, date_fin: event.target.value })} /></div>
      <div className="field full-width"><label htmlFor="import-zone">Fuseau de la période</label>
        <input id="import-zone" name="fuseau" required maxLength={64} value={period.fuseau}
          onChange={event => onChange({ ...period, fuseau: event.target.value })} /></div>
    </fieldset>
    <p>Messages reçus, envoyés et archivés uniquement. Aucun téléchargement de pièce jointe. Cet import n’active pas l’analyse Gemini.</p>
    <div className="actions"><button type="submit" disabled={busy}>Importer la période…</button></div>
  </form>
}

export function ImportProgress({ job }: { job: SyncRun }) {
  return <div className="sync-progress" role="status" aria-live="polite">
    <p>Import initial : {RUN_LABELS[job.status]}</p>
    <p>Du {job.date_debut} au {job.date_fin} inclusivement — {job.fuseau}</p>
    <p>{job.processed} examinés · {job.imported} importés · {job.duplicates} doublons évités · {job.skipped} ignorés · {job.incomplete} contenus incomplets</p>
    {['queued', 'running', 'paused'].includes(job.status) && <p>Traitement serveur par lots de dix messages. Le total est connu à la fin de la pagination ; fermer cette page n’annule pas l’import.</p>}
    {job.error_code && <p>{SYNC_ERROR_LABELS[job.error_code] ?? 'Le traitement a rencontré une erreur. Vérifiez la connexion et le suivi avant de relancer.'}</p>}
    {job.retry_after && <p>Reprise possible à partir du {new Date(job.retry_after).toLocaleString('fr-CA')}.</p>}
  </div>
}

export default function InitialImportPanel({ job, disabled, onPendingChange, onQueued }: {
  job: SyncRun | null; disabled: boolean; onPendingChange: (pending: boolean) => void; onQueued: (run: SyncRun) => void
}) {
  const [period, setPeriod] = useState<ImportPeriod>({ date_debut: '', date_fin: '', fuseau: 'America/Toronto' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const submitting = useRef(false)
  async function submit() {
    if (disabled || submitting.current) return
    const validation = validateImportPeriod(period)
    if (validation) { setError(validation); return }
    if (!window.confirm(`Importer les messages du ${period.date_debut} au ${period.date_fin} inclusivement (${period.fuseau}) ?`)) return
    submitting.current = true; setBusy(true); onPendingChange(true); setError(''); setNotice('')
    try {
      const run = await enqueueInitialImport(period)
      setNotice('Demande enregistrée. Le serveur poursuivra l’import ; aucune analyse IA n’a été activée.')
      onQueued(run)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Import indisponible.') }
    finally { submitting.current = false; setBusy(false); onPendingChange(false) }
  }
  const current = job
  const active = current && ['queued', 'running', 'paused'].includes(current.status)
  return <section aria-label="Import initial des courriels">
    <h3>Import initial Gmail</h3>
    {error && <p role="alert" className="error">{error}</p>}
    {notice && <p role="status" className="success">{notice}</p>}
    <InitialImportForm period={period} busy={disabled || busy || Boolean(active)} onChange={setPeriod} onSubmit={() => void submit()} />
    {current && <ImportProgress job={current} />}
  </section>
}
