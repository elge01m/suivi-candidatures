import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ETATS, listCandidatures } from './candidatures'
import type { Candidature } from './candidatures'
import {
  ACTIONS, FIELD_LABELS, correctProposal, decideProposal, draftFrom, isDirty,
  listPendingProposals, validateDraft,
} from './proposals'
import type { Proposal, ProposalDraft, ProposalErrors } from './proposals'

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Une erreur est survenue. Actualisez les données.'
}

function dateLabel(value: string | null): string {
  return value ? value.split('-').reverse().join('/') : 'Non renseignée'
}

const ASSOCIATION_LABELS = {
  aucune: 'Aucune correspondance suggérée', probable: 'Correspondance probable',
  a_confirmer: 'Correspondance partielle à confirmer', ambigu: 'Correspondances ambiguës',
}

export function AssociationSuggestions({ proposal, candidates }: { proposal: Proposal; candidates: Candidature[] }) {
  const p = proposal.proposition
  const suggested = candidates.filter(candidate => p.candidatures_possibles.includes(candidate.id))
  const level = p.niveau_association ?? (p.ambiguite ? 'ambigu' : suggested.length ? 'a_confirmer' : 'aucune')
  return <div className="proposal-guidance" aria-label="Suggestions d’association du serveur">
    <p><strong>{ASSOCIATION_LABELS[level]}</strong></p>
    {suggested.length > 0 && <ul>{suggested.map(candidate => <li key={candidate.id}>
      {candidate.employeur} — {candidate.poste} — {dateLabel(candidate.date_candidature)} — {ETATS.find(state => state.value === candidate.etat)?.label}
    </li>)}</ul>}
    {p.candidatures_possibles.length > 0 && !suggested.length && <p>Les correspondances ne sont plus disponibles. Actualisez les données.</p>}
    <p>Suggestions calculées sur l’employeur ou le poste, sans certitude automatique. Même probable, une correspondance doit être choisie par vous. Vous pouvez sélectionner une autre candidature.</p>
  </div>
}

type ReviewProps = {
  proposal: Proposal
  draft: ProposalDraft
  candidates: Candidature[]
  busy: boolean
  error: string
  errors: ProposalErrors
  onChange: (draft: ProposalDraft) => void
  onSave: () => void
  onAccept: () => void
  onReject: () => void
  onCancel: () => void
}

// Controlled form: unknown values stay empty and no candidate is preselected.
export function ProposalForm({ proposal, draft, candidates, busy, error, errors,
  onChange, onSave, onAccept, onReject, onCancel }: ReviewProps) {
  const prefix = `proposal-${proposal.id}`
  const dirty = isDirty(proposal, draft)
  const readiness = validateDraft(draft, candidates, true)
  const suggested = candidates.filter(candidate => proposal.proposition.candidatures_possibles.includes(candidate.id))
  const others = candidates.filter(candidate => !proposal.proposition.candidatures_possibles.includes(candidate.id))
  function candidateOption(candidate: Candidature) {
    return <option key={candidate.id} value={String(candidate.id)}>
      {candidate.employeur} — {candidate.poste} — {dateLabel(candidate.date_candidature)}
    </option>
  }
  function update<K extends keyof ProposalDraft>(key: K, value: ProposalDraft[K]) {
    onChange({ ...draft, [key]: value, ...(key === 'action' ? { cible_id: '' } : {}) })
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!busy) onSave()
  }
  function feedback(field: keyof ProposalDraft) {
    return errors[field] ? <span id={`${prefix}-${field}-error`} className="field-error">{errors[field]}</span> : null
  }
  function accessibility(field: keyof ProposalDraft) {
    return { 'aria-invalid': Boolean(errors[field]), 'aria-describedby': errors[field] ? `${prefix}-${field}-error` : undefined }
  }
  return (
    <form onSubmit={submit} aria-label="Corriger et décider" aria-busy={busy} className="proposal-form">
      {error && <p className="error" role="alert">{error}</p>}
      <AssociationSuggestions proposal={proposal} candidates={candidates} />
      <fieldset disabled={busy} className="form-grid">
        <legend className="sr-only">Corrections de la proposition</legend>
        <div className="field full-width">
          <label htmlFor={`${prefix}-action`}>Action proposée</label>
          <select id={`${prefix}-action`} name="action" value={draft.action}
            onChange={event => update('action', event.target.value as ProposalDraft['action'])} {...accessibility('action')}>
            {ACTIONS.map(action => <option key={action.value} value={action.value}>{action.label}</option>)}
          </select>
          {feedback('action')}
        </div>
        {(['employeur', 'poste'] as const).map(field => <div className="field" key={field}>
          <label htmlFor={`${prefix}-${field}`}>{FIELD_LABELS[field]}</label>
          <input id={`${prefix}-${field}`} name={field} value={draft[field]} maxLength={200}
            placeholder="Non renseigné" onChange={event => update(field, event.target.value)} {...accessibility(field)} />
          {feedback(field)}
        </div>)}
        <div className="field">
          <label htmlFor={`${prefix}-date_candidature`}>Date de candidature</label>
          <input type="date" id={`${prefix}-date_candidature`} name="date_candidature" value={draft.date_candidature}
            onChange={event => update('date_candidature', event.target.value)} {...accessibility('date_candidature')} />
          <span className="optional">Laissez vide si inconnue. Ne saisissez pas la date d’entrevue.</span>
          {feedback('date_candidature')}
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-etat`}>État proposé</label>
          <select id={`${prefix}-etat`} name="etat" value={draft.etat}
            disabled={draft.action === 'confirmation_reception'}
            onChange={event => update('etat', event.target.value as ProposalDraft['etat'])} {...accessibility('etat')}>
            <option value="">Non renseigné</option>
            {ETATS.map(state => <option key={state.value} value={state.value}>{state.label}</option>)}
          </select>
          {feedback('etat')}
          {draft.action === 'confirmation_reception' && <span className="optional">La confirmation conserve l’état existant.</span>}
        </div>
        <div className="field">
          <label htmlFor={`${prefix}-confirmation_reception`}>Confirmation de réception</label>
          <select id={`${prefix}-confirmation_reception`} name="confirmation_reception" value={draft.confirmation_reception}
            onChange={event => update('confirmation_reception', event.target.value as ProposalDraft['confirmation_reception'])} {...accessibility('confirmation_reception')}>
            <option value="">Non renseignée</option><option value="true">Oui</option><option value="false">Non</option>
          </select>
          <span className="optional">Indépendante de l’état. Non renseignée conserve une confirmation existante. Pour modifier uniquement cet accusé, choisissez « Confirmer la réception ».</span>
          {feedback('confirmation_reception')}
        </div>
        {['changement_etat', 'confirmation_reception'].includes(draft.action) && <div className="field full-width">
          <label htmlFor={`${prefix}-cible_id`}>Candidature à modifier — choix obligatoire</label>
          <select id={`${prefix}-cible_id`} name="cible_id" value={draft.cible_id}
            onChange={event => update('cible_id', event.target.value)} {...accessibility('cible_id')}>
            <option value="">Sélectionnez vous-même une candidature</option>
            {suggested.length > 0 && <optgroup label="Suggestions du serveur — à vérifier">{suggested.map(candidateOption)}</optgroup>}
            {others.length > 0 && <optgroup label="Autres candidatures — choix libre">{others.map(candidateOption)}</optgroup>}
          </select>
          {!candidates.length && <p className="proposal-hint">Aucune candidature existante. Vous pouvez corriger l’action en « Créer une candidature » et compléter les champs, ou rejeter cette proposition.</p>}
          <span className="optional">{draft.action === 'confirmation_reception' ? 'Seule la confirmation sera modifiée ; l’état et la date seront conservés.' : 'L’état et une confirmation explicitement renseignée seront modifiés. Si la date manque, renseignez-la pour quitter À préparer. Une date déjà connue sera conservée.'}</span>
          {feedback('cible_id')}
        </div>}
      </fieldset>
      <div className="proposal-guidance" aria-live="polite">
        {dirty && <p>Corrections non enregistrées : enregistrez-les, puis relisez la proposition avant de l’accepter.</p>}
        {Object.keys(readiness).length > 0 && <div>
          <p>Avant acceptation :</p>
          <ul>{Object.entries(readiness).map(([key, value]) => <li key={key}>{value}</li>)}</ul>
        </div>}
      </div>
      <div className="actions">
        <button type="submit" className="secondary" disabled={busy}>Enregistrer les corrections</button>
        <button type="button" disabled={busy || dirty || Object.keys(readiness).length > 0} onClick={onAccept}>Accepter la proposition…</button>
        <button type="button" className="danger" disabled={busy} onClick={onReject}>Rejeter la proposition…</button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>Fermer</button>
      </div>
      {busy && <p role="status">Traitement en cours…</p>}
    </form>
  )
}

function ProposalReview({ proposal, candidates, busy, error, onSave, onAccept, onReject, onCancel }: {
  proposal: Proposal; candidates: Candidature[]; busy: boolean; error: string
  onSave: (draft: ProposalDraft) => void; onAccept: () => void; onReject: (dirty: boolean) => void; onCancel: () => void
}) {
  const [draft, setDraft] = useState(() => draftFrom(proposal))
  const [errors, setErrors] = useState<ProposalErrors>({})
  useEffect(() => { document.getElementById(`proposal-${proposal.id}-action`)?.focus() }, [proposal.id, proposal.revision])
  return <ProposalForm proposal={proposal} draft={draft} candidates={candidates} busy={busy} error={error} errors={errors}
    onChange={value => { setDraft(value); setErrors({}) }}
    onSave={() => {
      const validation = validateDraft(draft, candidates)
      setErrors(validation)
      if (!Object.keys(validation).length) onSave(draft)
    }}
    onAccept={() => {
      const validation = validateDraft(draft, candidates, true)
      setErrors(validation)
      if (!isDirty(proposal, draft) && !Object.keys(validation).length) onAccept()
    }}
    onReject={() => onReject(isDirty(proposal, draft))}
    onCancel={() => { if (!isDirty(proposal, draft) || window.confirm('Fermer sans enregistrer les corrections ?')) onCancel() }} />
}

type Props = {
  disabled: boolean
  candidatesVersion: number
  onPendingChange: (pending: boolean) => void
  onAccepted: () => void
}

export default function ProposalsPanel({ disabled, candidatesVersion, onPendingChange, onAccepted }: Props) {
  const [items, setItems] = useState<Proposal[]>([])
  const [candidates, setCandidates] = useState<Candidature[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const mutation = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const locked = disabled || busy || loading

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([listPendingProposals(controller.signal), listCandidatures(controller.signal)])
      .then(([proposals, applications]) => {
        if (!controller.signal.aborted) {
          setItems(proposals)
          setCandidates(applications)
          setSelected(current => proposals.some(p => p.id === current) ? current : null)
          setLoadError('')
        }
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setLoadError(`${message(error)} Les décisions sont suspendues jusqu’à l’actualisation.`)
      }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [reload, candidatesVersion])

  function refresh() {
    setLoading(true)
    setActionError('')
    setSelected(null)
    setReload(value => value + 1)
  }

  async function mutate(proposal: Proposal, action: 'save' | 'accept' | 'reject', draft?: ProposalDraft, dirty = false) {
    if (locked || loadError || mutation.current) return
    mutation.current = true
    setBusy(true)
    onPendingChange(true)
    setActionError('')
    setNotice('')
    try {
      let result: Proposal | null
      const target = candidates.find(candidate => candidate.id === proposal.proposition.cible_id)
      if (action === 'save' && draft) {
        result = await correctProposal(proposal, draft, candidates)
      } else if (action === 'accept' || action === 'reject') {
        result = await decideProposal(proposal, action, () => window.confirm(action === 'accept'
          ? proposal.proposition.action === 'creation'
            ? `Créer une candidature « ${proposal.proposition.poste} » chez ${proposal.proposition.employeur} avec les informations enregistrées ?`
            : proposal.proposition.action === 'confirmation_reception'
              ? `Enregistrer la confirmation de réception « ${proposal.proposition.confirmation_reception ? 'Oui' : 'Non'} » pour « ${target?.poste} » chez ${target?.employeur}, sans changer l’état ?`
              : `Appliquer l’état « ${ETATS.find(s => s.value === proposal.proposition.etat)?.label} » à la candidature « ${target?.poste} » chez ${target?.employeur}, du ${dateLabel(target?.date_candidature ?? null)} ? Confirmation de réception : ${proposal.proposition.confirmation_reception == null ? 'inchangée' : proposal.proposition.confirmation_reception ? 'Oui' : 'Non'}.`
          : `Rejeter cette proposition sans modifier de candidature ?${dirty ? ' Les corrections non enregistrées seront abandonnées.' : ''}`), candidates)
      } else return
      if (!result) return
      if (action === 'save') {
        setItems(current => current.map(item => item.id === result.id ? result : item))
        setNotice('Corrections enregistrées. Aucune candidature modifiée. Relisez la proposition avant de confirmer son acceptation.')
      } else {
        setItems(current => current.filter(item => item.id !== result.id))
        setSelected(null)
        setNotice(action === 'accept' ? 'Proposition acceptée. Votre décision a été appliquée.' : 'Proposition rejetée. Aucune candidature modifiée.')
        heading.current?.focus()
        if (action === 'accept') onAccepted()
      }
      // Re-read the list after each successful write; never repeat the write on read failure.
      setLoading(true)
      setReload(value => value + 1)
    } catch (error) {
      setActionError(message(error))
    } finally {
      mutation.current = false
      setBusy(false)
      onPendingChange(false)
    }
  }

  return (
    <section className="proposals" aria-labelledby="proposals-heading" aria-busy={busy || loading}>
      <div className="list-toolbar">
        <h2 id="proposals-heading" tabIndex={-1} ref={heading}>Propositions IA à valider</h2>
        <button className="secondary" type="button" disabled={locked} onClick={() => {
          if (!selected || window.confirm('Actualiser les données ? Les corrections non enregistrées seront abandonnées.')) refresh()
        }}>Actualiser les propositions</button>
      </div>
      <p className="human-review-note">Aucune proposition n’est appliquée sans votre acceptation explicite. Vous pouvez corriger les informations ou rejeter une proposition. Cette page ne lance aucune analyse IA.</p>
      {notice && <p className="success" role="status">{notice}</p>}
      {actionError && !selected && <p className="error" role="alert">{actionError}</p>}
      {loadError && <p className="error" role="alert">{loadError}</p>}
      {loading && <p role="status">Chargement des propositions…</p>}
      {!loading && !loadError && !items.length && <div className="empty-state"><h3>Aucune proposition en attente</h3><p>Les propositions déjà acceptées ou rejetées ne figurent pas dans cette liste.</p></div>}
      {!loadError && <ul className="application-list">
        {items.map(proposal => {
          const p = proposal.proposition
          return <li key={proposal.id} className="application-card proposal-card">
            <div className="application-heading">
              <div><h3>{p.poste || 'Poste non renseigné'}</h3><p className="employer">{p.employeur || 'Employeur non renseigné'}</p></div>
              <span className="state-badge">En attente de votre décision</span>
            </div>
            <dl className="proposal-facts">
              <div><dt>Action proposée</dt><dd>{ACTIONS.find(a => a.value === p.action)?.label}</dd></div>
              <div><dt>Association suggérée</dt><dd>{ASSOCIATION_LABELS[p.niveau_association ?? (p.ambiguite ? 'ambigu' : p.candidatures_possibles.length ? 'a_confirmer' : 'aucune')]}</dd></div>
              <div><dt>État proposé</dt><dd>{ETATS.find(s => s.value === p.etat)?.label ?? 'Non renseigné'}</dd></div>
              <div><dt>Date de candidature</dt><dd>{dateLabel(p.date_candidature)}</dd></div>
              <div><dt>Confirmation de réception</dt><dd>{p.confirmation_reception == null ? 'Non renseignée' : p.confirmation_reception ? 'Oui' : 'Non'}</dd></div>
              <div><dt>Informations manquantes</dt><dd>{p.informations_manquantes.length ? p.informations_manquantes.map(field => FIELD_LABELS[field]).join(', ') : 'Aucune signalée'}</dd></div>
            </dl>
            <p className="proposal-justification">{p.justification}</p>
            {p.ambiguite && <p className="proposal-warning">Plusieurs candidatures peuvent correspondre. Choisissez vous-même la candidature à modifier.</p>}
            {p.correspondances_tronquees && <p className="proposal-warning">La liste des correspondances possibles est limitée. Vérifiez votre choix parmi vos candidatures.</p>}
            {p.contenu_incomplet && <p className="proposal-warning">Le texte source était incomplet. Vérifiez les informations avec attention.</p>}
            {!p.pertinent && <p className="proposal-warning">Le modèle a signalé ce message comme non pertinent.</p>}
            {selected === proposal.id ? <ProposalReview key={`${proposal.id}-${proposal.revision}`}
              proposal={proposal} candidates={candidates} busy={locked} error={actionError}
              onSave={draft => void mutate(proposal, 'save', draft)}
              onAccept={() => void mutate(proposal, 'accept')}
              onReject={dirty => void mutate(proposal, 'reject', undefined, dirty)}
              onCancel={() => { setSelected(null); setActionError(''); heading.current?.focus() }} />
              : <button type="button" className="secondary" disabled={locked || selected !== null} onClick={() => {
                setSelected(proposal.id); setActionError(''); setNotice('')
              }}>Examiner la proposition</button>}
          </li>
        })}
      </ul>}
    </section>
  )
}
