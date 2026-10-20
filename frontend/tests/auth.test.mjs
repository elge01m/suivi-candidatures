import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import { getSession, login, logout } from '../src/auth.ts'

const user = { id: 1, username: 'test-user' }
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

beforeEach(() => {
  globalThis.document = { cookie: '' }
})
afterEach(() => {
  mock.restoreAll()
  delete globalThis.document
})

test('restores an existing session and sends cookies without caching', async () => {
  mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/auth/me/')
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.cache, 'no-store')
    return json({ ...user, ignored: 'not retained' })
  })
  assert.deepEqual(await getSession(), user)
})

test('a 401 session response means logged out', async () => {
  mock.method(globalThis, 'fetch', async () => json({}, 401))
  assert.equal(await getSession(), null)
})

test('network failures are distinguished from a logged-out session', async () => {
  mock.method(globalThis, 'fetch', async () => { throw new TypeError('network') })
  await assert.rejects(getSession(), /injoignable/)
})

test('server failures are not treated as a logged-out session', async () => {
  mock.method(globalThis, 'fetch', async () => json({}, 500))
  await assert.rejects(getSession(), /requête a échoué/)
})

test('unexpected responses are rejected', async () => {
  mock.method(globalThis, 'fetch', async () => new Response('<html>proxy failure</html>'))
  await assert.rejects(getSession(), /inattendue/)
  mock.restoreAll()
  mock.method(globalThis, 'fetch', async () => json({ username: 'missing-id' }))
  await assert.rejects(getSession(), /inattendue/)
})

test('cancelled session requests preserve the abort signal', async () => {
  const controller = new AbortController()
  controller.abort()
  mock.method(globalThis, 'fetch', async (_url, options) => {
    assert.equal(options.signal.aborted, true)
    throw new DOMException('Aborted', 'AbortError')
  })
  await assert.rejects(getSession(controller.signal), { name: 'AbortError' })
})

test('login initializes CSRF and logout uses the rotated cookie', async () => {
  const calls = []
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url)
    assert.equal(options.credentials, 'same-origin')
    if (url === '/api/auth/csrf/') {
      if (!document.cookie) document.cookie = 'csrftoken=before-login'
      return json({ detail: 'OK' })
    }
    if (url === '/api/auth/login/') {
      assert.equal(options.method, 'POST')
      assert.equal(options.headers['X-CSRFToken'], 'before-login')
      assert.equal(options.headers['Content-Type'], 'application/json')
      assert.deepEqual(JSON.parse(options.body), { username: 'test-user', password: 'test-only' })
      document.cookie = 'csrftoken=after-login'
      return json(user)
    }
    assert.equal(url, '/api/auth/logout/')
    assert.equal(options.method, 'POST')
    assert.equal(options.headers['X-CSRFToken'], 'after-login')
    return new Response(null, { status: 204 })
  })
  assert.deepEqual(await login('test-user', 'test-only'), user)
  await logout()
  assert.deepEqual(calls, ['/api/auth/csrf/', '/api/auth/login/', '/api/auth/csrf/', '/api/auth/logout/'])
})

test('login rereads CSRF immediately after Django rotates it', async () => {
  mock.method(globalThis, 'fetch', async (url) => {
    if (url === '/api/auth/csrf/') {
      document.cookie = 'csrftoken=initial'
      return json({})
    }
    document.cookie = ''
    return json(user)
  })
  await assert.rejects(login('test-user', 'test-only'), /cookies/)
})

test('a missing CSRF cookie prevents login', async () => {
  const fetchMock = mock.method(globalThis, 'fetch', async () => json({}))
  await assert.rejects(login('test-user', 'test-only'), /cookies/)
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('a CSRF initialization failure prevents submission of credentials', async () => {
  const fetchMock = mock.method(globalThis, 'fetch', async () => json({}, 503))
  await assert.rejects(login('test-user', 'test-only'), /requête a échoué/)
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('invalid credentials produce a generic message', async () => {
  mock.method(globalThis, 'fetch', async (url) => {
    document.cookie = 'csrftoken=test-token'
    return url === '/api/auth/csrf/' ? json({}) : json({}, 401)
  })
  await assert.rejects(login('test-user', 'test-only'), /Identifiant ou mot de passe incorrect/)
})

test('a CSRF rejection is reported and is not silently retried', async () => {
  const fetchMock = mock.method(globalThis, 'fetch', async (url) => {
    document.cookie = 'anothercookie=value; csrftoken=current'
    return url === '/api/auth/csrf/' ? json({}) : json({}, 403)
  })
  await assert.rejects(login('test-user', 'test-only'), /protection/)
  assert.equal(fetchMock.mock.callCount(), 2)
})

test('logout failures are reported to keep the UI from claiming logout succeeded', async () => {
  mock.method(globalThis, 'fetch', async (url) => {
    document.cookie = 'csrftoken=current'
    return url === '/api/auth/csrf/' ? json({}) : json({}, 500)
  })
  await assert.rejects(logout(), /requête a échoué/)
})
