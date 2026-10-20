import { ApiError, initializeCsrf, request } from './auth.ts'

export const RUN_LABELS = {
  queued: 'En attente du serveur', running: 'En cours', paused: 'En pause',
  completed: 'Terminée', failed: 'Échec', cancelled: 'Annulée',
} as const
export type SyncRun = {
  id: string; status: keyof typeof RUN_LABELS; origin: 'manual' | 'scheduled' | 'initial'; error_code: string
  date_debut: string; date_fin: string; fuseau: string; incomplete: number
  processed: number; imported: number; duplicates: number; skipped: number; retry_after: string | null
}
export type SyncSettings = {
  connected: true; gmail_enabled: boolean; auto_analysis_enabled: boolean; interval_minutes: 5
  timezone: string; revision: number; consent_at: string | null; next_due_at: string | null
  last_success_at: string | null; blocked_code: string; analysis_pending: number; analyses_completed: number
  analyses_failed: number; last_analysis_error: string
  latest_run: SyncRun | null; latest_initial_import: SyncRun | null
}
export type SyncConfiguration = SyncSettings | { connected: false }
export type SyncDraft = Pick<SyncSettings, 'gmail_enabled' | 'auto_analysis_enabled' | 'timezone'> & { consent: boolean }

const MESSAGES: Record<string, string> = {
  consent_required: 'Confirmez explicitement la transmission des nouveaux messages à Gemini.',
  stale_revision: 'Les paramètres ont changé. Actualisez-les avant de réessayer.',
  invalid_request: 'Vérifiez la période, le fuseau et les options.',
  invalid_period: 'Choisissez des dates valides, avec une fin après le début ou le même jour.',
  invalid_timezone: 'Choisissez un fuseau horaire valide.',
  busy: 'Un autre import ou une synchronisation est déjà en cours.',
  not_connected: 'Connectez votre compte Gmail avant de synchroniser.',
  sync_blocked: 'La synchronisation est bloquée. Vérifiez votre connexion Gmail.',
}
export const SYNC_ERROR_LABELS: Record<string, string> = {
  reauthorization_required: 'Une reconnexion Gmail est nécessaire.',
  mailbox_changed: 'La boîte Google a changé. Vérifiez la connexion avant de reprendre.',
  gmail_access_denied: 'Google refuse l’accès à cette boîte.',
  rate_limited: 'Limite API atteinte ; reprise différée.',
  gmail_unavailable: 'Gmail est temporairement indisponible.',
  credentials_changed: 'Les identifiants ont changé. Réactivez la synchronisation après vérification.',
  quota_exceeded: 'Budget local atteint ; les analyses attendent leur prochain créneau disponible.',
  provider_unavailable: 'Gemini est temporairement indisponible ; reprise différée.',
  provider_denied: 'L’accès à Gemini est refusé. Vérifiez la configuration du serveur.',
  configuration_missing: 'L’analyse n’est pas configurée sur le serveur.',
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
function unexpected(): never { throw new Error('Réponse de synchronisation inattendue. Actualisez les données.') }
function natural(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 }
function nullableDate(value: unknown): value is string | null {
  return value === null || typeof value === 'string' && !Number.isNaN(Date.parse(value))
}

export function parseRun(value: unknown): SyncRun {
  if (!object(value) || typeof value.id !== 'string' || !/^[0-9a-f-]{36}$/i.test(value.id) ||
      typeof value.status !== 'string' || !Object.hasOwn(RUN_LABELS, value.status) ||
      !['manual', 'scheduled', 'initial'].includes(String(value.origin)) || typeof value.error_code !== 'string' ||
      !['processed','imported','duplicates','skipped','incomplete'].every(key => natural(value[key])) || !nullableDate(value.retry_after) || typeof value.date_debut !== 'string' || typeof value.date_fin !== 'string' || typeof value.fuseau !== 'string') return unexpected()
  return { id: value.id, status: value.status as SyncRun['status'], origin: value.origin as SyncRun['origin'],
    date_debut: value.date_debut as string, date_fin: value.date_fin as string, fuseau: value.fuseau as string, incomplete: value.incomplete as number,
    error_code: value.error_code, processed: value.processed as number, imported: value.imported as number,
    duplicates: value.duplicates as number, skipped: value.skipped as number, retry_after: value.retry_after }
}

export function parseConfiguration(value: unknown): SyncConfiguration {
  if (!object(value) || typeof value.connected !== 'boolean') return unexpected()
  if (!value.connected) return { connected: false }
  if (typeof value.gmail_enabled !== 'boolean' || typeof value.auto_analysis_enabled !== 'boolean' ||
      value.interval_minutes !== 5 || typeof value.timezone !== 'string' ||
      !natural(value.revision) || value.revision < 1 || !nullableDate(value.consent_at) || !nullableDate(value.next_due_at) ||
      !nullableDate(value.last_success_at) || typeof value.blocked_code !== 'string' || !natural(value.analysis_pending) ||
      !natural(value.analyses_completed) || !natural(value.analyses_failed) || typeof value.last_analysis_error !== 'string') return unexpected()
  return { connected: true, gmail_enabled: value.gmail_enabled, auto_analysis_enabled: value.auto_analysis_enabled,
    interval_minutes: 5, timezone: value.timezone, revision: value.revision,
    consent_at: value.consent_at, next_due_at: value.next_due_at, last_success_at: value.last_success_at,
    blocked_code: value.blocked_code, analysis_pending: value.analysis_pending, analyses_completed: value.analyses_completed,
    analyses_failed: value.analyses_failed, last_analysis_error: value.last_analysis_error,
    latest_initial_import: value.latest_initial_import === null ? null : parseRun(value.latest_initial_import),
    latest_run: value.latest_run === null ? null : parseRun(value.latest_run) }
}

export function syncDraft(config: SyncSettings): SyncDraft {
  return { gmail_enabled: config.gmail_enabled, auto_analysis_enabled: config.auto_analysis_enabled,
    timezone: config.timezone, consent: false }
}

export function validateSyncDraft(config: SyncSettings, draft: SyncDraft): string {
  try { new Intl.DateTimeFormat('fr-CA', { timeZone: draft.timezone }) } catch { return MESSAGES.invalid_request }
  if (draft.auto_analysis_enabled && !config.auto_analysis_enabled && draft.consent !== true) return MESSAGES.consent_required
  return ''
}

async function syncRequest(path: string, options: RequestInit = {}): Promise<unknown> {
  try {
    const response = await request(path, options)
    try { return await response.json() } catch { return unexpected() }
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 401 || error.status === 403) throw new Error('Session expirée ou protection CSRF refusée. Reconnectez-vous si nécessaire.')
      throw new Error(error.code && Object.hasOwn(MESSAGES, error.code) ? MESSAGES[error.code] : 'Synchronisation indisponible. Actualisez les données avant de réessayer.')
    }
    throw error
  }
}

export async function getSyncConfiguration(signal?: AbortSignal): Promise<SyncConfiguration> {
  return parseConfiguration(await syncRequest('/api/sync/configuration/', { signal }))
}

export async function saveSyncConfiguration(config: SyncSettings, draft: SyncDraft): Promise<SyncConfiguration> {
  const error = validateSyncDraft(config, draft)
  if (error) throw new Error(error)
  const token = await initializeCsrf()
  return parseConfiguration(await syncRequest('/api/sync/configuration/', {
    method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
    body: JSON.stringify({ revision: config.revision, gmail_enabled: draft.gmail_enabled,
      auto_analysis_enabled: draft.auto_analysis_enabled,
      timezone: draft.timezone, consent: draft.consent === true }),
  }))
}

export async function enqueueSync(): Promise<SyncRun> {
  const token = await initializeCsrf()
  return parseRun(await syncRequest('/api/sync/runs/', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token }, body: '{}',
  }))
}

export type ImportPeriod = { date_debut: string; date_fin: string; fuseau: string }

export function validateImportPeriod(period: ImportPeriod): string {
  for (const value of [period.date_debut, period.date_fin]) {
    const parsed = new Date(`${value}T12:00:00Z`)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000') ||
        Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) return MESSAGES.invalid_period
  }
  if (period.date_debut > period.date_fin || period.date_fin === '9999-12-31') return MESSAGES.invalid_period
  try { new Intl.DateTimeFormat('fr-CA', { timeZone: period.fuseau }) } catch { return MESSAGES.invalid_timezone }
  return ''
}

export async function enqueueInitialImport(period: ImportPeriod): Promise<SyncRun> {
  const error = validateImportPeriod(period)
  if (error) throw new Error(error)
  const token = await initializeCsrf()
  return parseRun(await syncRequest('/api/sync/imports/', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
    body: JSON.stringify({ date_debut: period.date_debut, date_fin: period.date_fin, fuseau: period.fuseau }),
  }))
}
