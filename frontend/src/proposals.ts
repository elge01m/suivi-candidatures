import { ApiError, initializeCsrf, request } from './auth.ts'
import { ETATS } from './candidatures.ts'
import type { Candidature, Etat } from './candidatures.ts'

export const ACTIONS = [
  { value: 'aucune', label: 'Aucune action' },
  { value: 'creation', label: 'Créer une candidature' },
  { value: 'changement_etat', label: 'Changer un état' },
  { value: 'confirmation_reception', label: 'Confirmer la réception' },
] as const
export const FIELD_LABELS = {
  employeur: 'Employeur', poste: 'Poste', date_candidature: 'Date de candidature', etat: 'État',
} as const
export type Action = typeof ACTIONS[number]['value']
export type ProposalStatus = 'en_attente' | 'acceptee' | 'rejetee'
export type AssociationLevel = 'aucune' | 'probable' | 'a_confirmer' | 'ambigu'
export type Proposal = {
  id: string
  status: ProposalStatus
  revision: number
  resultat_id: number | null
  proposition: {
    pertinent: boolean
    action: Action
    employeur: string | null
    poste: string | null
    date_candidature: string | null
    etat: Etat | null
    confirmation_reception: boolean | null
    justification: string
    informations_manquantes: (keyof typeof FIELD_LABELS)[]
    candidatures_possibles: number[]
    niveau_association: AssociationLevel
    ambiguite: boolean
    correspondances_tronquees: boolean
    cible_id: number | null
    cible_version: string | null
    contenu_incomplet: boolean
  }
}
export type ProposalDraft = {
  action: Action; employeur: string; poste: string; date_candidature: string
  etat: Etat | ''; cible_id: string; confirmation_reception: '' | 'true' | 'false'
}
export type ProposalErrors = Partial<Record<keyof ProposalDraft, string>>

const ERROR_MESSAGES: Record<string, string> = {
  invalid_fields: 'Vérifiez la date et la confirmation : une candidature à préparer ne peut pas être confirmée reçue.',
  invalid_request: 'Vérifiez les champs de la proposition.',
  not_found: 'La proposition ou la candidature est introuvable. Actualisez les données.',
  already_decided: 'Cette proposition a déjà été traitée. Actualisez la liste.',
  stale_revision: 'La proposition a changé. Actualisez-la et relisez-la avant de confirmer.',
  target_required: 'Sélectionnez explicitement une candidature existante.',
  target_changed: 'La candidature a changé. Actualisez les données, puis enregistrez à nouveau votre choix.',
  missing_fields: 'Complétez les informations obligatoires pour cette action.',
  duplicate_creation: 'Une candidature correspondante existe déjà. Sélectionnez plutôt un changement d’état si nécessaire.',
}

export class ProposalError extends Error {
  code?: string
  constructor(message: string, code?: string) {
    super(message)
    this.code = code
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
function positive(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}
function uuid(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value)
}
function status(value: unknown): value is ProposalStatus {
  return value === 'en_attente' || value === 'acceptee' || value === 'rejetee'
}
function validDate(value: string): boolean {
  const parsed = new Date(`${value}T12:00:00Z`)
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && value.slice(0, 4) !== '0000' &&
    !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
}
function unexpected(): never {
  throw new ProposalError('Le serveur a renvoyé une proposition inattendue. Actualisez la liste.')
}

export function parseProposal(value: unknown): Proposal {
  if (!record(value) || !uuid(value.id) || !status(value.status) || !positive(value.revision) ||
      !(value.resultat_id === null || positive(value.resultat_id)) || !record(value.proposition)) return unexpected()
  const p = value.proposition
  if (typeof p.pertinent !== 'boolean' || !ACTIONS.some(a => a.value === p.action) ||
      !['employeur', 'poste'].every(k => p[k] === null || typeof p[k] === 'string' && p[k].length <= 200) ||
      !(p.date_candidature === null || typeof p.date_candidature === 'string' && validDate(p.date_candidature)) ||
      !(p.etat === null || ETATS.some(e => e.value === p.etat)) || typeof p.justification !== 'string' ||
      !(p.confirmation_reception === undefined || p.confirmation_reception === null || typeof p.confirmation_reception === 'boolean') ||
      !Array.isArray(p.informations_manquantes) || !p.informations_manquantes.every(k => typeof k === 'string' && Object.hasOwn(FIELD_LABELS, k)) ||
      !Array.isArray(p.candidatures_possibles) || !p.candidatures_possibles.every(positive) ||
      !(p.niveau_association === undefined || ['aucune', 'probable', 'a_confirmer', 'ambigu'].includes(String(p.niveau_association))) ||
      !['ambiguite','correspondances_tronquees','contenu_incomplet'].every(k => typeof p[k] === 'boolean') ||
      !(p.cible_id === null || positive(p.cible_id)) || !(p.cible_version === null || typeof p.cible_version === 'string')) return unexpected()
  // Whitelist: do not keep unexpected metadata or ownership fields in UI state.
  return {
    id: value.id, status: value.status, revision: value.revision, resultat_id: value.resultat_id,
    proposition: {
      pertinent: p.pertinent, action: p.action as Action,
      employeur: p.employeur as string | null, poste: p.poste as string | null,
      date_candidature: p.date_candidature as string | null, etat: p.etat as Etat | null,
      confirmation_reception: p.confirmation_reception as boolean | null ?? null,
      justification: p.justification,
      informations_manquantes: p.informations_manquantes as (keyof typeof FIELD_LABELS)[],
      candidatures_possibles: p.candidatures_possibles as number[], ambiguite: p.ambiguite as boolean,
      niveau_association: p.niveau_association as AssociationLevel ?? (p.ambiguite ? 'ambigu' : p.candidatures_possibles.length ? 'a_confirmer' : 'aucune'),
      correspondances_tronquees: p.correspondances_tronquees as boolean,
      cible_id: p.cible_id as number | null, cible_version: p.cible_version as string | null,
      contenu_incomplet: p.contenu_incomplet as boolean,
    },
  }
}

export function draftFrom(proposal: Proposal): ProposalDraft {
  const p = proposal.proposition
  return { action: p.action, employeur: p.employeur ?? '', poste: p.poste ?? '',
    date_candidature: p.date_candidature ?? '', etat: p.etat ?? '',
    confirmation_reception: p.confirmation_reception == null ? '' : p.confirmation_reception ? 'true' : 'false',
    // Only restore an explicitly saved human choice. Never use possible matches.
    cible_id: p.cible_id === null ? '' : String(p.cible_id) }
}

export function validateDraft(draft: ProposalDraft, candidates: Candidature[], accepting = false): ProposalErrors {
  const errors: ProposalErrors = {}
  if (!ACTIONS.some(a => a.value === draft.action) || accepting && draft.action === 'aucune') errors.action = 'Choisissez une action applicable ou rejetez la proposition.'
  for (const field of ['employeur', 'poste'] as const) {
    if (draft[field].trim().length > 200 || accepting && draft.action === 'creation' && !draft[field].trim()) errors[field] = `${FIELD_LABELS[field]} requis pour une création (200 caractères maximum).`
  }
  const target = candidates.find(c => String(c.id) === draft.cible_id)
  const receiptOnly = draft.action === 'confirmation_reception'
  const updating = draft.action === 'changement_etat' || receiptOnly
  if (draft.date_candidature && !validDate(draft.date_candidature) || accepting && !receiptOnly && draft.etat !== 'a_preparer' && !draft.date_candidature &&
      (draft.action === 'creation' || updating && target && !target.date_candidature)) errors.date_candidature = 'Renseignez une date de candidature valide, distincte de la date d’entrevue.'
  if (draft.etat && !ETATS.some(e => e.value === draft.etat) || accepting && !receiptOnly && !draft.etat) errors.etat = 'Choisissez explicitement un état.'
  if (updating && (draft.cible_id && !target || accepting && !draft.cible_id)) errors.cible_id = 'Sélectionnez explicitement une candidature existante dans la liste.'
  if (draft.confirmation_reception !== undefined && !['', 'true', 'false'].includes(draft.confirmation_reception) || accepting && receiptOnly && !draft.confirmation_reception ||
      (['creation', 'changement_etat'].includes(draft.action) && draft.etat === 'a_preparer' || receiptOnly && target?.etat === 'a_preparer') && draft.confirmation_reception === 'true' ||
      draft.action === 'changement_etat' && draft.etat === 'a_preparer' && target?.confirmation_reception === true && draft.confirmation_reception !== 'false') errors.confirmation_reception = 'Renseignez la confirmation. Une candidature à préparer ne peut pas être confirmée reçue.'
  return errors
}

export function isDirty(proposal: Proposal, draft: ProposalDraft): boolean {
  const original = draftFrom(proposal)
  return (Object.keys(original) as (keyof ProposalDraft)[]).some(key => original[key] !== draft[key])
}

async function proposalRequest(path: string, options: RequestInit = {}): Promise<unknown> {
  try {
    const response = await request(path, options)
    try { return await response.json() } catch { return unexpected() }
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 401 || error.status === 403) throw new ProposalError('Session expirée ou requête refusée. Reconnectez-vous si nécessaire.')
      const explanation = error.code && Object.hasOwn(ERROR_MESSAGES, error.code) ? ERROR_MESSAGES[error.code] : undefined
      throw new ProposalError(explanation ?? 'La proposition n’a pas pu être traitée. Actualisez les données avant de réessayer.', error.code)
    }
    throw error
  }
}

function path(id: string): string {
  if (!uuid(id)) throw new ProposalError('Identifiant de proposition invalide.')
  return `/api/propositions/${id}/`
}

export async function listPendingProposals(signal?: AbortSignal): Promise<Proposal[]> {
  const body = await proposalRequest('/api/propositions/', { signal })
  if (!record(body) || !Array.isArray(body.results)) return unexpected()
  const ids: string[] = []
  for (const row of body.results) {
    if (!record(row) || !uuid(row.id) || !status(row.status) || !positive(row.revision)) return unexpected()
    if (row.status === 'en_attente') ids.push(row.id)
  }
  const result: Proposal[] = []
  // Bound simultaneous reads when the backend returns up to 100 metadata rows.
  for (let index = 0; index < ids.length; index += 4) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    const page = await Promise.all(ids.slice(index, index + 4).map(async id => {
      const proposal = parseProposal(await proposalRequest(path(id), { signal }))
      if (proposal.id !== id) return unexpected()
      return proposal
    }))
    result.push(...page.filter(p => p.status === 'en_attente'))
  }
  return result
}

export async function correctProposal(proposal: Proposal, draft: ProposalDraft, candidates: Candidature[]): Promise<Proposal> {
  if (Object.keys(validateDraft(draft, candidates)).length) throw new ProposalError('Vérifiez les champs de la proposition.')
  if (proposal.status !== 'en_attente') throw new ProposalError('Cette proposition a déjà été traitée.')
  const url = path(proposal.id)
  const token = await initializeCsrf()
  const result = parseProposal(await proposalRequest(url, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
    body: JSON.stringify({ revision: proposal.revision, proposition: {
      action: draft.action, employeur: draft.employeur.trim() || null, poste: draft.poste.trim() || null,
      date_candidature: draft.date_candidature || null, etat: draft.etat || null,
      confirmation_reception: draft.confirmation_reception ? draft.confirmation_reception === 'true' : null,
      cible_id: ['changement_etat', 'confirmation_reception'].includes(draft.action) && draft.cible_id ? Number(draft.cible_id) : null,
    } }),
  }))
  if (result.id !== proposal.id) return unexpected()
  return result
}

export async function decideProposal(proposal: Proposal, action: 'accept' | 'reject', confirm: () => boolean, candidates: Candidature[] = []): Promise<Proposal | null> {
  if (proposal.status !== 'en_attente') throw new ProposalError('Cette proposition a déjà été traitée.')
  if (!['accept', 'reject'].includes(action)) throw new ProposalError('Décision invalide.')
  if (action === 'accept') {
    const errors = validateDraft(draftFrom(proposal), candidates, true)
    if (Object.keys(errors).length) throw new ProposalError(Object.values(errors).join(' '))
  }
  if (!confirm()) return null
  const url = path(proposal.id) + action + '/'
  const token = await initializeCsrf()
  const result = parseProposal(await proposalRequest(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
    body: JSON.stringify({ revision: proposal.revision, confirmer: true }),
  }))
  if (result.id !== proposal.id || result.status !== (action === 'accept' ? 'acceptee' : 'rejetee')) return unexpected()
  return result
}
