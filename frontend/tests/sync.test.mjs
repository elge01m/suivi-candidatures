import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import { enqueueSync, getSyncConfiguration, parseConfiguration, saveSyncConfiguration, syncDraft, validateSyncDraft } from '../src/sync.ts'

export const configuration = {
  connected:true, gmail_enabled:false, auto_analysis_enabled:false, interval_minutes:5,
  timezone:'America/Toronto', revision:1, consent_at:null, next_due_at:null, last_success_at:null,
  blocked_code:'', analysis_pending:0, analyses_completed:0, analyses_failed:0, last_analysis_error:'', latest_run:null, latest_initial_import:null,
}
export const run = {
  id:'12345678-1234-4234-8234-123456789abc', status:'queued', origin:'manual', error_code:'',
  date_debut:'2026-10-12', date_fin:'2026-10-12', fuseau:'America/Toronto', incomplete:0, processed:0, imported:0, duplicates:0, skipped:0, retry_after:null,
}
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers:{'Content-Type':'application/json'} })
beforeEach(() => { globalThis.document = {cookie:'csrftoken=initial-token'} })
afterEach(() => { mock.restoreAll(); delete globalThis.document })

test('default form preserves disabled automation and never pre-consents', () => {
  const draft = syncDraft(configuration)
  assert.equal(draft.auto_analysis_enabled, false)
  assert.equal(draft.gmail_enabled, false)
  assert.equal(draft.consent, false)
  assert.equal(draft.timezone, 'America/Toronto')
})

test('enabling analysis requires a fresh explicit consent', async () => {
  const draft = {...syncDraft(configuration), auto_analysis_enabled:true}
  assert.match(validateSyncDraft(configuration, draft), /explicitement/)
  const fetch = mock.method(globalThis,'fetch',async () => {throw new Error('Must not call')})
  await assert.rejects(saveSyncConfiguration(configuration, draft), /explicitement/)
  assert.equal(fetch.mock.callCount(),0)
})

test('five-minute cadence is fixed and timezone is validated', () => {
  assert.ok(validateSyncDraft(configuration,{...syncDraft(configuration),timezone:'nonexistent-zone'}))
  assert.equal(validateSyncDraft(configuration,syncDraft(configuration)),'')
  assert.equal(parseConfiguration(configuration).interval_minutes,5)
  assert.equal(Object.hasOwn(syncDraft(configuration),'time_1'),false)
})

test('reading config uses session and makes no Gmail, Gemini or processing call', async () => {
  const calls=[]
  mock.method(globalThis,'fetch',async (path,options) => {
    calls.push(path)
    assert.equal(options.credentials,'same-origin')
    assert.equal(options.cache,'no-store')
    assert.equal(options.method,undefined)
    return json(configuration)
  })
  assert.deepEqual(await getSyncConfiguration(),configuration)
  assert.deepEqual(calls,['/api/sync/configuration/'])
})

test('enqueue returns immediately with server job, using CSRF, and never polls in the API helper', async () => {
  const calls=[]
  mock.method(globalThis,'fetch',async (path,options) => {
    calls.push(path)
    if(path==='/api/auth/csrf/') { document.cookie='csrftoken=current-token'; return json({}) }
    assert.equal(path,'/api/sync/runs/')
    assert.equal(options.method,'POST')
    assert.equal(options.headers['X-CSRFToken'],'current-token')
    assert.equal(options.body,'{}')
    return json(run,202)
  })
  assert.deepEqual(await enqueueSync(),run)
  assert.deepEqual(calls,['/api/auth/csrf/','/api/sync/runs/'])
})

test('settings whitelist ownership, send revision and explicit consent only on user save', async () => {
  const draft={...syncDraft(configuration),auto_analysis_enabled:true,consent:true,utilisateur:999}
  mock.method(globalThis,'fetch',async (path,options) => {
    if(path==='/api/auth/csrf/') return json({})
    assert.equal(options.method,'PATCH')
    assert.equal(options.credentials,'same-origin')
    assert.equal(options.headers['X-CSRFToken'],'initial-token')
    assert.deepEqual(JSON.parse(options.body),{revision:1,gmail_enabled:false,auto_analysis_enabled:true,timezone:'America/Toronto',consent:true})
    return json({...configuration,revision:2,auto_analysis_enabled:true,consent_at:'2026-10-19T10:00:00Z'})
  })
  const result=await saveSyncConfiguration(configuration,draft)
  assert.equal(result.auto_analysis_enabled,true)
})

test('revocation sends disabled analysis without needing new consent', async () => {
  const enabled={...configuration,auto_analysis_enabled:true,consent_at:'2026-10-19T10:00:00Z'}
  const draft={...syncDraft(enabled),auto_analysis_enabled:false}
  mock.method(globalThis,'fetch',async (path,options) => {
    if(path==='/api/auth/csrf/') return json({})
    assert.equal(JSON.parse(options.body).auto_analysis_enabled,false)
    assert.equal(JSON.parse(options.body).consent,false)
    return json(configuration)
  })
  assert.equal((await saveSyncConfiguration(enabled,draft)).auto_analysis_enabled,false)
})

test('safe errors for stale config, session and CSRF do not expose raw server data', async () => {
  for (const [status,code,pattern] of [[409,'stale_revision',/paramètres ont changé/],[403,'forbidden',/CSRF/],[401,'authentication_required',/Session/]]) {
    mock.method(globalThis,'fetch',async () => json({code,detail:'PRIVATE'},status))
    await assert.rejects(getSyncConfiguration(),e => pattern.test(e.message) && !e.message.includes('PRIVATE'))
    mock.restoreAll()
  }
})

test('unconnected and malformed snapshots handled without enabling automation', async () => {
  assert.deepEqual(parseConfiguration({connected:false}),{connected:false})
  for(const change of [{revision:0},{interval_minutes:10},{analysis_pending:-1},{auto_analysis_enabled:'true'}]) {
    assert.throws(()=>parseConfiguration({...configuration,...change}))
  }
  assert.equal(parseConfiguration({...configuration,latest_run:run}).latest_run.status,'queued')
})

test('network interruption never retries a manual synchronization automatically', async () => {
  let writes=0
  mock.method(globalThis,'fetch',async path => {
    if(path==='/api/auth/csrf/') return json({})
    writes+=1
    throw new TypeError('Fictional network failure')
  })
  await assert.rejects(enqueueSync(),/injoignable/)
  assert.equal(writes,1)
})
