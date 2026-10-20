import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import { enqueueInitialImport, getSyncConfiguration, validateImportPeriod } from '../src/sync.ts'

const period = { date_debut:'2026-10-12', date_fin:'2026-10-12', fuseau:'America/Toronto' }
const run = { id:'12345678-1234-4234-8234-123456789abc', origin:'initial', status:'queued',
  ...period, error_code:'', processed:0, imported:0, duplicates:0, skipped:0, incomplete:0, retry_after:null }
const config = { connected:true, gmail_enabled:false, auto_analysis_enabled:false, interval_minutes:5,
  timezone:'America/Toronto', revision:1, consent_at:null, next_due_at:null, last_success_at:null,
  blocked_code:'', analysis_pending:0, analyses_completed:0, analyses_failed:0, last_analysis_error:'',
  latest_run:run, latest_initial_import:run }
const json = (value, status=200) => new Response(JSON.stringify(value), { status, headers:{'Content-Type':'application/json'} })
beforeEach(() => { globalThis.document = { cookie:'csrftoken=initial-token' } })
afterEach(() => { mock.restoreAll(); delete globalThis.document })

test('period validation accepts same day and leap dates without inventing defaults', () => {
  assert.equal(validateImportPeriod(period), '')
  assert.equal(validateImportPeriod({ ...period, date_debut:'2024-02-29' }), '')
  for (const change of [{date_debut:''}, {date_debut:'2026-02-30'}, {date_fin:'2026-10-11'},
    {date_fin:'0000-01-01'}, {date_fin:'9999-12-31'}, {fuseau:'invalid'}]) {
    assert.ok(validateImportPeriod({ ...period, ...change }))
  }
})

test('an invalid period never initializes CSRF or submits a request', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected request') })
  await assert.rejects(enqueueInitialImport({ ...period, date_debut:'' }))
  assert.equal(fetch.mock.callCount(), 0)
})

test('explicit enqueue sends only the selected period and refreshed CSRF, receives 202 without processing', async () => {
  const calls = []
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url)
    if (url === '/api/auth/csrf/') { document.cookie = 'csrftoken=refreshed'; return json({}) }
    assert.equal(url, '/api/sync/imports/')
    assert.equal(options.method, 'POST')
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.headers['X-CSRFToken'], 'refreshed')
    assert.deepEqual(JSON.parse(options.body), period)
    return json(run, 202)
  })
  assert.deepEqual(await enqueueInitialImport({ ...period, utilisateur:999, auto_analysis_enabled:true }), run)
  assert.deepEqual(calls, ['/api/auth/csrf/', '/api/sync/imports/'])
})

test('read-only status restores an initial import after reloading, without triggering work', async () => {
  const completed = { ...run, status:'completed', processed:12, imported:10, duplicates:2, incomplete:1 }
  mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/sync/configuration/')
    assert.equal(options.method, undefined)
    return json({ ...config, latest_initial_import:completed })
  })
  const result = await getSyncConfiguration()
  assert.deepEqual(result.latest_initial_import, completed)
  assert.equal(result.gmail_enabled, false)
  assert.equal(result.auto_analysis_enabled, false)
})

test('busy, invalid periods and CSRF failures expose safe errors only', async () => {
  for (const [status, code, pattern] of [[409,'busy',/déjà en cours/], [400,'invalid_period',/dates valides/], [403,'forbidden',/CSRF/]]) {
    mock.method(globalThis, 'fetch', async url => url === '/api/auth/csrf/' ? json({}) : json({code, detail:'PRIVATE'},status))
    await assert.rejects(enqueueInitialImport(period), error => pattern.test(error.message) && !error.message.includes('PRIVATE'))
    mock.restoreAll()
  }
})

test('a lost enqueue response never triggers an automatic retry or continue request', async () => {
  let writes = 0
  mock.method(globalThis, 'fetch', async url => {
    if (url === '/api/auth/csrf/') return json({})
    writes += 1
    throw new TypeError('Fictional network interruption')
  })
  await assert.rejects(enqueueInitialImport(period), /injoignable/)
  assert.equal(writes, 1)
})
