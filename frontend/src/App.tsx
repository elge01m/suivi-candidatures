import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { getSession, login, logout } from './auth'
import type { User } from './auth'
import Candidatures from './CandidaturesPanel'
import ProposalsPanel from './ProposalsPanel'
import SyncPanel from './SyncPanel'
import TrackingPanel from './TrackingPanel'
import './App.css'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Une erreur est survenue. Réessayez.'
}

function App() {
  const [user, setUser] = useState<User | null>(null)
  const [sessionState, setSessionState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [retry, setRetry] = useState(0)
  const [busy, setBusy] = useState(false)
  const [dataBusy, setDataBusy] = useState(false)
  const [proposalsBusy, setProposalsBusy] = useState(false)
  const [syncBusy, setSyncBusy] = useState(false)
  const [trackingBusy, setTrackingBusy] = useState(false)
  const [candidaturesVersion, setCandidaturesVersion] = useState(0)
  const [targetsVersion, setTargetsVersion] = useState(0)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    getSession(controller.signal).then((currentUser) => {
      if (!controller.signal.aborted) {
        setUser(currentUser)
        setSessionState('ready')
      }
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) {
        setError(errorMessage(cause))
        setSessionState('error')
      }
    })
    return () => controller.abort()
  }, [retry])

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const form = event.currentTarget
    const fields = new FormData(form)
    const username = String(fields.get('username') ?? '').trim()
    const password = String(fields.get('password') ?? '')
    if (!username || !password) {
      setError('Saisissez votre identifiant et votre mot de passe.')
      return
    }
    setBusy(true)
    setError('')
    const passwordInput = form.elements.namedItem('password')
    if (passwordInput instanceof HTMLInputElement) passwordInput.value = ''
    try {
      setUser(await login(username, password))
      form.reset()
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function handleLogout() {
    if (busy || dataBusy || proposalsBusy || syncBusy || trackingBusy) return
    setBusy(true)
    setError('')
    try {
      await logout()
      setUser(null)
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="auth-layout">
      <header className="brand"><span className="brand-mark" aria-hidden="true">SC</span>Suivi des candidatures</header>
      <section className={`auth-card ${user && sessionState === 'ready' ? 'workspace' : ''}`} aria-labelledby="page-title" aria-busy={busy || sessionState === 'loading'}>
        <p className="eyebrow">Espace personnel</p>
        {sessionState === 'loading' ? (
          <>
            <h1 id="page-title">Bienvenue</h1>
            <p role="status">Vérification de votre session…</p>
          </>
        ) : sessionState === 'error' ? (
          <>
            <h1 id="page-title">Connexion indisponible</h1>
            <p className="error" role="alert">{error}</p>
            <button onClick={() => {
              setSessionState('loading')
              setError('')
              setRetry((value) => value + 1)
            }}>Réessayer</button>
          </>
        ) : user ? (
          <>
            <div className="workspace-header">
              <div><h1 id="page-title">Mes candidatures</h1><p>Bonjour, {user.username}.</p></div>
              <button className="secondary" type="button" onClick={handleLogout} disabled={busy || dataBusy || proposalsBusy || syncBusy || trackingBusy}>
                {busy ? 'Déconnexion…' : 'Déconnexion'}
              </button>
            </div>
            {error && <p className="error" role="alert">{error}</p>}
            {busy && <span className="sr-only" role="status">Déconnexion en cours.</span>}
            <Candidatures disabled={busy || proposalsBusy} onPendingChange={setDataBusy}
              refreshVersion={candidaturesVersion} onChanged={() => setTargetsVersion(value => value + 1)} />
            <ProposalsPanel disabled={busy || dataBusy} onPendingChange={setProposalsBusy}
              candidatesVersion={targetsVersion} onAccepted={() => setCandidaturesVersion(value => value + 1)} />
            <SyncPanel disabled={busy || dataBusy || proposalsBusy} onPendingChange={setSyncBusy}
              onUpdated={() => setTargetsVersion(value => value + 1)} />
            <TrackingPanel disabled={busy || dataBusy || proposalsBusy || syncBusy} onPendingChange={setTrackingBusy}
              onUpdated={() => setTargetsVersion(value => value + 1)} />
          </>
        ) : (
          <>
            <h1 id="page-title">Connexion</h1>
            <p>Retrouvez votre espace de suivi personnel.</p>
            <form onSubmit={handleLogin}>
              <div className="field">
                <label htmlFor="username">Identifiant</label>
                <input id="username" name="username" type="text" autoComplete="username" autoCapitalize="none" spellCheck={false} required disabled={busy} />
              </div>
              <div className="field">
                <label htmlFor="password">Mot de passe</label>
                <input id="password" name="password" type="password" autoComplete="current-password" required disabled={busy} />
              </div>
              {error && <p className="error" role="alert">{error}</p>}
              <button type="submit" disabled={busy}>{busy ? 'Connexion…' : 'Se connecter'}</button>
              {busy && <span className="sr-only" role="status">Connexion en cours.</span>}
            </form>
            <p className="personal-note">Accès réservé à votre compte personnel.</p>
          </>
        )}
      </section>
      <footer>Votre parcours, à votre rythme.</footer>
    </main>
  )
}

export default App
