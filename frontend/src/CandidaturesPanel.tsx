import { useEffect, useRef, useState } from 'react'
import HistoryPanel from './HistoryPanel'
import type { FormEvent } from 'react'
import {
  CandidatureError, ETATS, deleteCandidature, emptyCandidature,
  listCandidatures, safeLink, saveCandidature,
} from './candidatures'
import type { Candidature, CandidatureInput, FieldErrors } from './candidatures'
import CandidatureFilters from './CandidatureFilterControls'
import { filterCandidatures } from './candidatureFilters'
import type { CandidatureCriteria } from './candidatureFilters'

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Une erreur est survenue. Réessayez.'
}

function frenchDate(value: string): string {
  const date = new Date(`${value}T12:00:00`)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('fr-CA', {
    day: 'numeric', month: 'long', year: 'numeric',
  })
}

type FormProps = {
  initial: Candidature | 'new'
  busy: boolean
  error: string
  fields: FieldErrors
  onSubmit: (value: CandidatureInput) => Promise<void>
  onCancel: () => void
}

export function CandidatureForm({ initial, busy, error, fields, onSubmit, onCancel }: FormProps) {
  const [value, setValue] = useState<CandidatureInput>(() => initial === 'new' ? emptyCandidature() : {
    employeur: initial.employeur, poste: initial.poste, date_candidature: initial.date_candidature,
    etat: initial.etat, confirmation_reception: initial.confirmation_reception, lien: initial.lien, notes: initial.notes,
  })
  const firstInput = useRef<HTMLInputElement>(null)
  const errorSummary = useRef<HTMLParagraphElement>(null)
  useEffect(() => { firstInput.current?.focus() }, [])
  useEffect(() => { if (error) errorSummary.current?.focus() }, [error])

  function update<K extends keyof CandidatureInput>(key: K, fieldValue: CandidatureInput[K]) {
    setValue((current) => ({ ...current, [key]: fieldValue }))
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!busy) void onSubmit(value)
  }

  return (
    <section className="editor" aria-labelledby="editor-title">
      <h2 id="editor-title">{initial === 'new' ? 'Ajouter une candidature' : 'Modifier la candidature'}</h2>
      <form onSubmit={submit} aria-busy={busy}>
        {error && <p className="error" role="alert" tabIndex={-1} ref={errorSummary}>{error}</p>}
        <fieldset disabled={busy} className="form-grid">
          <legend className="sr-only">Informations sur la candidature</legend>
          <div className="field">
            <label htmlFor="employeur">Employeur</label>
            <input ref={firstInput} id="employeur" name="employeur" required maxLength={200}
              value={value.employeur} onChange={(event) => update('employeur', event.target.value)}
              aria-invalid={Boolean(fields.employeur)} aria-describedby={fields.employeur ? 'employeur-error' : undefined} />
            {fields.employeur && <span className="field-error" id="employeur-error">{fields.employeur}</span>}
          </div>
          <div className="field">
            <label htmlFor="poste">Poste</label>
            <input id="poste" name="poste" required maxLength={200}
              value={value.poste} onChange={(event) => update('poste', event.target.value)}
              aria-invalid={Boolean(fields.poste)} aria-describedby={fields.poste ? 'poste-error' : undefined} />
            {fields.poste && <span className="field-error" id="poste-error">{fields.poste}</span>}
          </div>
          <div className="field">
            <label htmlFor="date_candidature">Date de candidature</label>
            <input id="date_candidature" name="date_candidature" type="date" required={value.etat !== 'a_preparer'}
              value={value.date_candidature ?? ''} onChange={(event) => update('date_candidature', event.target.value || null)}
              aria-invalid={Boolean(fields.date_candidature)} aria-describedby={fields.date_candidature ? 'date-error' : undefined} />
            {fields.date_candidature && <span className="field-error" id="date-error">{fields.date_candidature}</span>}
            <span className="optional">Facultative à l’état À préparer. Ne saisissez pas une date d’entrevue.</span>
          </div>
          <div className="field">
            <label htmlFor="etat">État</label>
            <select id="etat" name="etat" value={value.etat}
              onChange={(event) => update('etat', event.target.value as CandidatureInput['etat'])}
              aria-invalid={Boolean(fields.etat)} aria-describedby={fields.etat ? 'etat-error' : undefined}>
              {ETATS.map((state) => <option key={state.value} value={state.value}>{state.label}</option>)}
            </select>
            {fields.etat && <span className="field-error" id="etat-error">{fields.etat}</span>}
          </div>
          <div className="field">
            <label htmlFor="confirmation_reception">Confirmation de réception</label>
            <select id="confirmation_reception" name="confirmation_reception" value={value.confirmation_reception === null ? '' : String(value.confirmation_reception)}
              onChange={event => update('confirmation_reception', event.target.value === '' ? null : event.target.value === 'true')}
              aria-invalid={Boolean(fields.confirmation_reception)} aria-describedby={fields.confirmation_reception ? 'confirmation-error' : undefined}>
              <option value="">Non renseignée</option><option value="true">Oui</option><option value="false">Non</option>
            </select>
            <span className="optional">Indépendante de l’état Envoyée. Aucun accusé n’est supposé.</span>
            {fields.confirmation_reception && <span className="field-error" id="confirmation-error">{fields.confirmation_reception}</span>}
          </div>
          <div className="field full-width">
            <label htmlFor="lien">Lien <span className="optional">(facultatif)</span></label>
            <input id="lien" name="lien" type="url" maxLength={200} placeholder="https://…"
              value={value.lien} onChange={(event) => update('lien', event.target.value)}
              aria-invalid={Boolean(fields.lien)} aria-describedby={fields.lien ? 'lien-error' : undefined} />
            {fields.lien && <span className="field-error" id="lien-error">{fields.lien}</span>}
          </div>
          <div className="field full-width">
            <label htmlFor="notes">Notes <span className="optional">(facultatives)</span></label>
            <textarea id="notes" name="notes" rows={4}
              value={value.notes} onChange={(event) => update('notes', event.target.value)}
              aria-invalid={Boolean(fields.notes)} aria-describedby={fields.notes ? 'notes-error' : undefined} />
            {fields.notes && <span className="field-error" id="notes-error">{fields.notes}</span>}
          </div>
        </fieldset>
        <div className="actions">
          <button type="submit" disabled={busy}>{busy ? 'Enregistrement…' : 'Enregistrer'}</button>
          <button className="secondary" type="button" disabled={busy} onClick={onCancel}>Annuler</button>
        </div>
      </form>
    </section>
  )
}

type Props = { disabled: boolean; onPendingChange: (pending: boolean) => void; refreshVersion?: number; onChanged?: () => void }

export default function Candidatures({ disabled, onPendingChange, refreshVersion = 0, onChanged }: Props) {
  const [items, setItems] = useState<Candidature[]>([])
  const [criteria, setCriteria] = useState<CandidatureCriteria>({ search: '', etat: '' })
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [formError, setFormError] = useState('')
  const [fields, setFields] = useState<FieldErrors>({})
  const [notice, setNotice] = useState('')
  const [editor, setEditor] = useState<Candidature | 'new' | null>(null)
  const addButton = useRef<HTMLButtonElement>(null)
  const locked = disabled || busy || loading
  const visibleItems = filterCandidatures(items, criteria)

  useEffect(() => {
    const controller = new AbortController()
    listCandidatures(controller.signal).then((result) => {
      if (!controller.signal.aborted) setItems(result)
      if (!controller.signal.aborted) setLoadError('')
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setLoadError(message(error))
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [refreshVersion])

  async function refresh() {
    setLoading(true)
    setLoadError('')
    try {
      setItems(await listCandidatures())
    } catch (error) {
      setLoadError(`${message(error)} La liste affichée peut ne pas être à jour.`)
    } finally {
      setLoading(false)
    }
  }

  function openEditor(value: Candidature | 'new') {
    setFormError('')
    setFields({})
    setActionError('')
    setNotice('')
    setEditor(value)
  }

  function closeEditor() {
    setEditor(null)
    addButton.current?.focus()
  }

  async function save(value: CandidatureInput) {
    if (locked || editor === null) return
    setBusy(true)
    onPendingChange(true)
    setFormError('')
    setFields({})
    try {
      const saved = await saveCandidature(value, editor === 'new' ? undefined : editor.id)
      setItems((current) => [saved, ...current.filter((item) => item.id !== saved.id)]
        .sort((left, right) => (right.date_candidature ?? '').localeCompare(left.date_candidature ?? '') || right.id - left.id))
      setNotice(editor === 'new' ? 'Candidature ajoutée.' : 'Candidature modifiée.')
      closeEditor()
      onChanged?.()
      // The write is complete even if refreshing subsequently fails: do not resubmit it.
      await refresh()
    } catch (error) {
      setFormError(message(error))
      setFields(error instanceof CandidatureError ? error.fields : {})
    } finally {
      setBusy(false)
      onPendingChange(false)
    }
  }

  async function remove(item: Candidature) {
    if (locked) return
    setBusy(true)
    onPendingChange(true)
    setActionError('')
    setNotice('')
    try {
      const deleted = await deleteCandidature(item.id, () => window.confirm(
        `Supprimer la candidature « ${item.poste} » chez ${item.employeur} ? Cette action est définitive.`,
      ))
      if (deleted) {
        setItems((current) => current.filter((entry) => entry.id !== item.id))
        setNotice('Candidature supprimée.')
        onChanged?.()
        await refresh()
      }
    } catch (error) {
      setActionError(message(error))
    } finally {
      setBusy(false)
      onPendingChange(false)
    }
  }

  return (
    <section className="candidatures" aria-label="Gestion des candidatures" aria-busy={busy || loading}>
      <div className="list-toolbar">
        <h2>Vos candidatures</h2>
        <button className="secondary" disabled={locked || editor !== null} onClick={() => void refresh()}>Actualiser</button>
        <button ref={addButton} disabled={locked || editor !== null || Boolean(loadError)} onClick={() => openEditor('new')}>
          Ajouter une candidature
        </button>
      </div>
      <CandidatureFilters criteria={criteria} disabled={disabled || busy} onChange={setCriteria} />
      {notice && <p className="success" role="status">{notice}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
      {loadError && <div className="load-error">
        <p className="error" role="alert">{loadError}</p>
        <button className="secondary" disabled={locked} onClick={() => void refresh()}>Actualiser la liste</button>
      </div>}
      {loading && <p role="status">Chargement des candidatures…</p>}
      {editor !== null && <CandidatureForm key={editor === 'new' ? 'new' : editor.id}
        initial={editor} busy={locked} error={formError} fields={fields}
        onSubmit={save} onCancel={closeEditor} />}
      {!loading && !loadError && items.length === 0 && <div className="empty-state">
        <h3>Aucune candidature pour le moment</h3>
        <p>Ajoutez votre première candidature pour commencer votre suivi.</p>
      </div>}
      {!loading && !loadError && items.length > 0 && visibleItems.length === 0 && <div className="empty-state" role="status">
        <h3>Aucune candidature ne correspond à vos critères</h3>
        <p>Modifiez votre recherche ou réinitialisez les critères.</p>
      </div>}
      <ul className="application-list">
        {visibleItems.map((item) => <li className="application-card" key={item.id}>
          <div className="application-heading">
            <div><h3>{item.poste}</h3><p className="employer">{item.employeur}</p></div>
            <span className={`state-badge state-${item.etat}`}>{ETATS.find((state) => state.value === item.etat)?.label}</span>
          </div>
          <p className="application-date">{item.date_candidature ? <>Candidature du <time dateTime={item.date_candidature}>{frenchDate(item.date_candidature)}</time></> : 'Date de candidature non renseignée'}</p>
          <p>Confirmation de réception : {item.confirmation_reception === null ? 'Non renseignée' : item.confirmation_reception ? 'Oui' : 'Non'}</p>
          {safeLink(item.lien) && <a className="offer-link" href={safeLink(item.lien)} target="_blank" rel="noopener noreferrer">Ouvrir le lien de la candidature <span className="sr-only">(nouvel onglet)</span></a>}
          {item.notes && <details><summary>Notes</summary><p className="notes">{item.notes}</p></details>}
          <div className="actions card-actions">
            <button className="secondary" disabled={locked || editor !== null || Boolean(loadError)}
              onClick={() => openEditor(item)} aria-label={`Modifier ${item.poste} chez ${item.employeur}`}>Modifier</button>
            <button className="danger" disabled={locked || editor !== null || Boolean(loadError)}
              onClick={() => void remove(item)} aria-label={`Supprimer ${item.poste} chez ${item.employeur}`}>Supprimer</button>
          </div>
          <HistoryPanel id={item.id} version={item} />
        </li>)}
      </ul>
    </section>
  )
}
