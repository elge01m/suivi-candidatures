import { ApiError, initializeCsrf, request } from './auth.ts'

export const ETATS = [
  { value: 'a_preparer', label: 'À préparer' },
  { value: 'envoyee', label: 'Envoyée' },
  { value: 'entrevue', label: 'Entrevue' },
  { value: 'offre_recue', label: 'Offre reçue' },
  { value: 'offre_acceptee', label: 'Offre acceptée' },
  { value: 'offre_declinee', label: 'Offre déclinée' },
  { value: 'refusee', label: 'Refusée' },
  { value: 'retiree', label: 'Retirée' },
] as const

export type Etat = typeof ETATS[number]['value']
export type CandidatureInput = {
  employeur: string
  poste: string
  date_candidature: string | null
  confirmation_reception: boolean | null
  etat: Etat
  lien: string
  notes: string
}
export type Candidature = CandidatureInput & { id: number }
export type FieldErrors = Partial<Record<keyof CandidatureInput, string>>

const FIELD_MESSAGES: Record<keyof CandidatureInput, string> = {
  employeur: 'Saisissez un employeur de 200 caractères maximum.',
  poste: 'Saisissez un poste de 200 caractères maximum.',
  date_candidature: 'Saisissez une date valide.',
  etat: 'Choisissez un état proposé dans la liste.',
  confirmation_reception: 'Une candidature à préparer ne peut pas être confirmée reçue.',
  lien: 'Saisissez une adresse valide de 200 caractères maximum, ou laissez ce champ vide.',
  notes: 'Vérifiez le contenu des notes.',
}

export class CandidatureError extends Error {
  fields: FieldErrors

  constructor(message: string, fields: FieldErrors = {}) {
    super(message)
    this.fields = fields
  }
}

export function validateInput(input: CandidatureInput): FieldErrors {
  const errors: FieldErrors = {}
  if (!input.employeur.trim() || input.employeur.trim().length > 200) errors.employeur = FIELD_MESSAGES.employeur
  if (!input.poste.trim() || input.poste.trim().length > 200) errors.poste = FIELD_MESSAGES.poste
  const date = new Date(`${input.date_candidature}T12:00:00Z`)
  if (input.date_candidature ? (!/^\d{4}-\d{2}-\d{2}$/.test(input.date_candidature) || input.date_candidature.startsWith('0000') ||
      Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== input.date_candidature) : input.etat !== 'a_preparer') {
    errors.date_candidature = FIELD_MESSAGES.date_candidature
  }
  if (!ETATS.some((state) => state.value === input.etat)) errors.etat = FIELD_MESSAGES.etat
  if (!(input.confirmation_reception === null || typeof input.confirmation_reception === 'boolean') ||
      input.etat === 'a_preparer' && input.confirmation_reception === true) errors.confirmation_reception = FIELD_MESSAGES.confirmation_reception
  if (input.lien.trim() && (!safeLink(input.lien.trim()) || input.lien.trim().length > 200)) errors.lien = FIELD_MESSAGES.lien
  return errors
}

export function safeLink(value: string): string | undefined {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined
  } catch {
    return undefined
  }
}

export function emptyCandidature(): CandidatureInput {
  const today = new Date()
  const date = [today.getFullYear(), String(today.getMonth() + 1).padStart(2, '0'),
    String(today.getDate()).padStart(2, '0')].join('-')
  return { employeur: '', poste: '', date_candidature: date, etat: 'envoyee', confirmation_reception: null, lien: '', notes: '' }
}

function parseCandidature(value: unknown): Candidature {
  if (typeof value !== 'object' || value === null || !('id' in value) ||
      !Number.isSafeInteger(value.id) || (value.id as number) < 1 ||
      !('employeur' in value) || typeof value.employeur !== 'string' ||
      !('poste' in value) || typeof value.poste !== 'string' ||
      !('date_candidature' in value) || !(value.date_candidature === null || typeof value.date_candidature === 'string') ||
      !('confirmation_reception' in value) || !(value.confirmation_reception === null || typeof value.confirmation_reception === 'boolean') ||
      !('etat' in value) || !ETATS.some((state) => state.value === value.etat) ||
      !('lien' in value) || typeof value.lien !== 'string' ||
      !('notes' in value) || typeof value.notes !== 'string') {
    throw new Error('Le serveur a renvoyé une candidature inattendue. Réessayez.')
  }
  return {
    id: value.id as number, employeur: value.employeur, poste: value.poste,
    date_candidature: value.date_candidature, etat: value.etat as Etat,
    confirmation_reception: value.confirmation_reception,
    lien: value.lien, notes: value.notes,
  }
}

function detailPath(id: number): string {
  if (!Number.isSafeInteger(id) || id < 1) throw new Error('Candidature invalide.')
  return `/api/candidatures/${id}/`
}

async function handleErrors<T>(action: () => Promise<T>): Promise<T> {
  try {
    return await action()
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 400) {
        const fields: FieldErrors = {}
        for (const field of Object.keys(FIELD_MESSAGES) as (keyof CandidatureInput)[]) {
          if (error.fields.includes(field)) fields[field] = FIELD_MESSAGES[field]
        }
        throw new CandidatureError('Certains champs sont invalides. Vérifiez le formulaire.', fields)
      }
      if (error.status === 401 || error.status === 403) {
        throw new CandidatureError('Accès refusé ou session expirée. Réessayez ou déconnectez-vous pour vous reconnecter.')
      }
      if (error.status === 404) {
        throw new CandidatureError('Cette candidature est introuvable. Actualisez la liste.')
      }
    }
    throw error
  }
}

async function jsonBody(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    throw new Error('Le serveur a renvoyé une réponse inattendue. Réessayez.')
  }
}

export async function listCandidatures(signal?: AbortSignal): Promise<Candidature[]> {
  return handleErrors(async () => {
    const response = await request('/api/candidatures/', { signal })
    const body = await jsonBody(response)
    if (!Array.isArray(body)) throw new Error('La liste reçue est invalide. Réessayez.')
    return body.map(parseCandidature)
  })
}

export async function saveCandidature(input: CandidatureInput, id?: number): Promise<Candidature> {
  const fields = validateInput(input)
  if (Object.keys(fields).length) throw new CandidatureError('Vérifiez les champs du formulaire.', fields)
  // Explicit whitelist: never submit utilisateur, id or server-managed dates.
  const body: CandidatureInput = {
    employeur: input.employeur.trim(), poste: input.poste.trim(),
    date_candidature: input.date_candidature || null, etat: input.etat,
    confirmation_reception: input.confirmation_reception,
    lien: input.lien.trim(), notes: input.notes,
  }
  return handleErrors(async () => {
    const path = id === undefined ? '/api/candidatures/' : detailPath(id)
    const token = await initializeCsrf()
    const response = await request(path, {
      method: id === undefined ? 'POST' : 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
      body: JSON.stringify(body),
    })
    return parseCandidature(await jsonBody(response))
  })
}

export async function deleteCandidature(id: number, confirm: () => boolean): Promise<boolean> {
  if (!confirm()) return false
  return handleErrors(async () => {
    const path = detailPath(id)
    const token = await initializeCsrf()
    await request(path, { method: 'DELETE', headers: { 'X-CSRFToken': token } })
    return true
  })
}
