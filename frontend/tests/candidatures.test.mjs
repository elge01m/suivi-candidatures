import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import {
  CandidatureError, ETATS, deleteCandidature, emptyCandidature,
  listCandidatures, safeLink, saveCandidature, validateInput,
} from '../src/candidatures.ts'

const input = {
  employeur: 'Employeur test', poste: 'Poste test', date_candidature: '2026-10-18',
  etat: 'envoyee', confirmation_reception: null, lien: '', notes: '',
}
const candidature = { id: 7, ...input }
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

beforeEach(() => { globalThis.document = { cookie: 'csrftoken=current-token' } })
afterEach(() => { mock.restoreAll(); delete globalThis.document })

test('eight states have the agreed French labels', () => {
  assert.deepEqual(ETATS.map((state) => state.value), [
    'a_preparer', 'envoyee', 'entrevue', 'offre_recue', 'offre_acceptee', 'offre_declinee', 'refusee', 'retiree',
  ])
  assert.deepEqual(ETATS.map((state) => state.label), [
    'À préparer', 'Envoyée', 'Entrevue', 'Offre reçue', 'Offre acceptée', 'Offre déclinée', 'Refusée', 'Retirée',
  ])
})

test('preparation permits no date, but sending requires an explicit valid date', () => {
  assert.deepEqual(validateInput({ ...input, etat:'a_preparer', date_candidature:null }), {})
  assert.ok(validateInput({ ...input, date_candidature:null }).date_candidature)
  assert.ok(validateInput({ ...input, etat:'a_preparer', confirmation_reception:true }).confirmation_reception)
  assert.deepEqual(validateInput({ ...input, confirmation_reception:true }), {})
  assert.deepEqual(validateInput({ ...input, etat:'entrevue', confirmation_reception:false }), {})
  assert.ok(validateInput({ ...input, date_candidature:'0000-01-01' }).date_candidature)
})

test('null preparation date and explicit receipt are transmitted with CSRF and parsed', async () => {
  const draft = { ...input, etat:'a_preparer', date_candidature:null, confirmation_reception:null }
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/auth/csrf/') return json({})
    assert.deepEqual(JSON.parse(options.body), draft)
    assert.equal(options.headers['X-CSRFToken'], 'current-token')
    return json({ id:7, ...draft })
  })
  assert.deepEqual(await saveCandidature(draft), { id:7, ...draft })
})

test('new applications default to sent, local date and empty optional fields', () => {
  const value = emptyCandidature()
  assert.equal(value.etat, 'envoyee')
  assert.equal(value.notes, '')
  assert.equal(value.lien, '')
  const now = new Date()
  assert.equal(Number(value.date_candidature.slice(0, 4)), now.getFullYear())
  assert.equal(Number(value.date_candidature.slice(5, 7)), now.getMonth() + 1)
  assert.equal(Number(value.date_candidature.slice(8)), now.getDate())
})

test('lists applications using the session and strips ownership metadata', async () => {
  mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/candidatures/')
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.cache, 'no-store')
    return json([{ ...candidature, utilisateur: 42, date_creation: 'server-managed' }])
  })
  assert.deepEqual(await listCandidatures(), [candidature])
})

test('an empty application list is supported', async () => {
  mock.method(globalThis, 'fetch', async () => json([]))
  assert.deepEqual(await listCandidatures(), [])
})

test('create sends only allowed fields and the latest CSRF cookie', async () => {
  const calls = []
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url)
    if (url === '/api/auth/csrf/') {
      document.cookie = 'csrftoken=renewed-token'
      return json({})
    }
    assert.equal(options.method, 'POST')
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.headers['X-CSRFToken'], 'renewed-token')
    assert.deepEqual(JSON.parse(options.body), input)
    return json(candidature, 201)
  })
  const result = await saveCandidature({ ...input, employeur: ' Employeur test ',
    utilisateur: 999, id: 999, date_creation: 'untrusted', date_modification: 'untrusted' })
  assert.deepEqual(result, candidature)
  assert.deepEqual(calls, ['/api/auth/csrf/', '/api/candidatures/'])
})

test('update uses PUT on the selected application and cannot transfer ownership', async () => {
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/auth/csrf/') return json({})
    assert.equal(url, '/api/candidatures/7/')
    assert.equal(options.method, 'PUT')
    assert.equal(options.headers['X-CSRFToken'], 'current-token')
    assert.deepEqual(JSON.parse(options.body), { ...input, etat: 'entrevue' })
    return json({ ...candidature, etat: 'entrevue' })
  })
  assert.equal((await saveCandidature({ ...input, etat: 'entrevue', utilisateur: 999 }, 7)).etat, 'entrevue')
})

test('cancelling deletion makes no network request', async () => {
  const fetchMock = mock.method(globalThis, 'fetch', async () => { throw new Error('must not fetch') })
  assert.equal(await deleteCandidature(7, () => false), false)
  assert.equal(fetchMock.mock.callCount(), 0)
})

test('confirmed deletion sends DELETE with CSRF and accepts a 204 response', async () => {
  let confirmations = 0
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/auth/csrf/') return json({})
    assert.equal(url, '/api/candidatures/7/')
    assert.equal(options.method, 'DELETE')
    assert.equal(options.headers['X-CSRFToken'], 'current-token')
    assert.equal(options.body, undefined)
    return new Response(null, { status: 204 })
  })
  assert.equal(await deleteCandidature(7, () => { confirmations += 1; return true }), true)
  assert.equal(confirmations, 1)
})

test('every write is followed by a fresh, uncached list when the UI reloads', async () => {
  const calls = []
  let records = []
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push([url, options.method ?? 'GET'])
    if (url === '/api/auth/csrf/') return json({})
    if (options.method === 'POST') { records = [candidature]; return json(candidature, 201) }
    if (options.method === 'PUT') { records = [{ ...candidature, etat: 'entrevue' }]; return json(records[0]) }
    if (options.method === 'DELETE') { records = []; return new Response(null, { status: 204 }) }
    assert.equal(options.cache, 'no-store')
    return json(records)
  })
  await saveCandidature(input)
  assert.equal((await listCandidatures()).length, 1)
  await saveCandidature({ ...input, etat: 'entrevue' }, 7)
  assert.equal((await listCandidatures())[0].etat, 'entrevue')
  await deleteCandidature(7, () => true)
  assert.deepEqual(await listCandidatures(), [])
  assert.deepEqual(calls.filter(([url]) => url !== '/api/auth/csrf/').map(([, method]) => method),
    ['POST', 'GET', 'PUT', 'GET', 'DELETE', 'GET'])
})

test('invalid inputs are rejected before any network request', async () => {
  const fetchMock = mock.method(globalThis, 'fetch', async () => json({}))
  const invalid = [
    [{ ...input, employeur: ' ' }, 'employeur'],
    [{ ...input, poste: 'x'.repeat(201) }, 'poste'],
    [{ ...input, date_candidature: '2026-02-30' }, 'date_candidature'],
    [{ ...input, date_candidature: 'invalid' }, 'date_candidature'],
    [{ ...input, etat: 'unknown' }, 'etat'],
    [{ ...input, lien: 'javascript:alert(1)' }, 'lien'],
  ]
  for (const [value, field] of invalid) {
    await assert.rejects(saveCandidature(value), (error) => error instanceof CandidatureError && Boolean(error.fields[field]))
  }
  assert.equal(fetchMock.mock.callCount(), 0)
  assert.deepEqual(validateInput(input), {})
  assert.deepEqual(validateInput({ ...input, date_candidature: '2024-02-29' }), {})
})

test('server field errors are mapped to French without echoing raw responses', async () => {
  mock.method(globalThis, 'fetch', async (url) => url === '/api/auth/csrf/' ? json({}) :
    json({ employeur: ['raw backend detail'], lien: ['Enter a valid URL.'] }, 400))
  await assert.rejects(saveCandidature(input), (error) => {
    assert.ok(error instanceof CandidatureError)
    assert.match(error.fields.employeur, /employeur/)
    assert.match(error.fields.lien, /adresse valide/)
    assert.doesNotMatch(error.message, /raw backend/)
    return true
  })
})

test('expired sessions, missing records and server failures are reported', async () => {
  for (const [status, expected] of [[401, /session expirée/], [403, /session expirée/],
    [404, /introuvable/], [500, /requête a échoué/]]) {
    mock.method(globalThis, 'fetch', async () => json({}, status))
    await assert.rejects(listCandidatures(), expected)
    mock.restoreAll()
  }
})

test('network failures and unexpected response shapes are rejected', async () => {
  mock.method(globalThis, 'fetch', async () => { throw new TypeError('offline') })
  await assert.rejects(listCandidatures(), /injoignable/)
  mock.restoreAll()
  for (const body of [{ results: [] }, [{ ...candidature, etat: 'unknown' }], [{ id: 7 }]]) {
    mock.method(globalThis, 'fetch', async () => json(body))
    await assert.rejects(listCandidatures(), /invalide|inattendue/)
    mock.restoreAll()
  }
  mock.method(globalThis, 'fetch', async () => new Response('<html>proxy error</html>'))
  await assert.rejects(listCandidatures(), /réponse inattendue/)
})

test('CSRF initialization failures prevent writes', async () => {
  const fetchMock = mock.method(globalThis, 'fetch', async () => json({}, 503))
  await assert.rejects(saveCandidature(input), /requête a échoué/)
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('only safe web links can be opened from application cards', () => {
  assert.equal(safeLink('javascript:alert(1)'), undefined)
  assert.equal(safeLink('data:text/html,test'), undefined)
  assert.equal(safeLink('not a url'), undefined)
  assert.equal(safeLink('https://example.com/job'), 'https://example.com/job')
})

test('invalid application identifiers cannot be used for writes', async () => {
  const fetchMock = mock.method(globalThis, 'fetch', async () => json({}))
  await assert.rejects(deleteCandidature(-1, () => true), /invalide/)
  await assert.rejects(saveCandidature(input, 1.5), /invalide/)
  assert.equal(fetchMock.mock.callCount(), 0)
})
