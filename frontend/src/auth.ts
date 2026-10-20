export type User = { id: number; username: string }

export class ApiError extends Error {
  status: number
  fields: string[]
  code?: string

  constructor(status: number, message: string, fields: string[] = [], code?: string) {
    super(message)
    this.status = status
    this.fields = fields
    this.code = code
  }
}

function csrfToken(): string {
  const value = document.cookie.split(';').map((part) => part.trim())
    .find((part) => part.startsWith('csrftoken='))?.slice('csrftoken='.length)
  if (!value) {
    throw new Error('La protection de connexion est indisponible. Autorisez les cookies et réessayez.')
  }
  return value
}

export async function request(path: string, options: RequestInit = {}): Promise<Response> {
  let response: Response
  try {
    const timeout = AbortSignal.timeout(10_000)
    response = await fetch(path, {
      ...options,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: options.signal ? AbortSignal.any([options.signal, timeout]) : timeout,
    })
  } catch {
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    throw new Error('Le serveur est injoignable ou met trop de temps à répondre. Réessayez.')
  }
  if (!response.ok) {
    const message = response.status === 403
      ? 'La protection de la requête a expiré ou a été refusée. Réessayez.'
      : response.status === 401
        ? 'Identifiant ou mot de passe incorrect.'
        : 'La requête a échoué. Réessayez dans un instant.'
    let fields: string[] = []
    let code: string | undefined
    if (response.status === 400 || path.startsWith('/api/propositions/') || path.startsWith('/api/sync/') || path.startsWith('/api/analyses/') || path.startsWith('/api/gmail/imports/')) {
      try {
        const body: unknown = await response.json()
        if (typeof body === 'object' && body !== null && !Array.isArray(body)) {
          if (response.status === 400) fields = Object.keys(body)
          if ('code' in body && typeof body.code === 'string' && /^[a-z_]{1,40}$/.test(body.code)) code = body.code
        }
      } catch { /* A non-JSON error response still gets a safe, generic message. */ }
    }
    throw new ApiError(response.status, message, fields, code)
  }
  return response
}

async function userFrom(response: Response): Promise<User> {
  let value: unknown
  try {
    value = await response.json()
  } catch {
    throw new Error('Le serveur a renvoyé une réponse inattendue. Réessayez.')
  }
  if (typeof value !== 'object' || value === null || !('id' in value) ||
      !('username' in value) || typeof value.id !== 'number' || typeof value.username !== 'string') {
    throw new Error('Le serveur a renvoyé une réponse inattendue. Réessayez.')
  }
  return { id: value.id, username: value.username }
}

export async function initializeCsrf(): Promise<string> {
  await request('/api/auth/csrf/')
  return csrfToken()
}

export async function getSession(signal?: AbortSignal): Promise<User | null> {
  try {
    return await userFrom(await request('/api/auth/me/', { signal }))
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null
    throw error
  }
}

export async function login(username: string, password: string): Promise<User> {
  const token = await initializeCsrf()
  const response = await request('/api/auth/login/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
    body: JSON.stringify({ username, password }),
  })
  // Django rotates this cookie at login. Never retain the pre-login value.
  csrfToken()
  return userFrom(response)
}

export async function logout(): Promise<void> {
  const token = await initializeCsrf()
  await request('/api/auth/logout/', {
    method: 'POST',
    headers: { 'X-CSRFToken': token },
  })
}
