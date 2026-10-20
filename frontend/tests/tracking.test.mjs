import assert from 'node:assert/strict'
import { afterEach,beforeEach,mock,test } from 'node:test'
import { listHistory,listTreatments,retryTreatment } from '../src/tracking.ts'

const id='12345678-1234-4234-8234-123456789abc'
const analysis={id,status:'failed',error_code:'provider_unavailable',retry_after:null,can_retry:true,attempts:1,message_reference:5}
const job={id,status:'paused',error_code:'gmail_unavailable',retry_after:null,can_retry:true,
  date_debut:'2026-10-12',date_fin:'2026-10-12',processed:3,imported:2,duplicates:1,skipped:0}
const entry={id:1,date:'2026-10-19T19:00:00Z',origine:'proposition',avant:{etat:'envoyee'},apres:{etat:'entrevue'},
  source_message_reference:5,source_message_id:null,source_proposition_reference:id}
const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})
beforeEach(()=>{globalThis.document={cookie:'csrftoken=initial-token'}})
afterEach(()=>{mock.restoreAll();delete globalThis.document})

test('history is a read-only session request and strips unexpected metadata',async()=>{
  mock.method(globalThis,'fetch',async(url,options)=>{
    assert.equal(url,'/api/candidatures/7/historique/')
    assert.equal(options.method,undefined)
    assert.equal(options.credentials,'same-origin')
    return json({results:[{...entry,secret:'DO NOT KEEP'}]})
  })
  assert.deepEqual(await listHistory(7),[entry])
})
test('history rejects ownership, unsafe identifiers and mail content in snapshots',async()=>{
  await assert.rejects(listHistory(-1))
  mock.method(globalThis,'fetch',async()=>json({results:[{...entry,apres:{subject:'PRIVATE'}}]}))
  await assert.rejects(listHistory(7),/inattendue/)
})
test('tracking reads both lists without sending mail or triggering processing',async()=>{
  const calls=[]
  mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push(url);assert.equal(options.method,undefined)
    return json({results:[url==='/api/analyses/'?{...analysis,secret:'PRIVATE'}:job]})
  })
  const rows=await listTreatments()
  assert.equal(rows.length,2)
  assert.deepEqual(calls.sort(),['/api/analyses/','/api/gmail/imports/'])
  assert.equal(rows[0].kind,'import')
  assert.equal(rows[1].kind,'analysis')
  assert.equal(Object.hasOwn(rows[1],'secret'),false)
})
test('cancelled or forbidden retries perform no request or consent initialization',async()=>{
  const fetch=mock.method(globalThis,'fetch',async()=>{throw new Error('Unexpected request')})
  assert.equal(await retryTreatment({...analysis,kind:'analysis'},()=>false),false)
  await assert.rejects(retryTreatment({...analysis,kind:'analysis',can_retry:false},()=>true))
  assert.equal(fetch.mock.callCount(),0)
})
test('analysis retry sends fresh explicit consent and CSRF only after confirmation',async()=>{
  mock.method(globalThis,'fetch',async(url,options)=>{
    if(url==='/api/auth/csrf/'){document.cookie='csrftoken=refreshed';return json({})}
    assert.equal(url,`/api/analyses/${id}/retry/`)
    assert.equal(options.headers['X-CSRFToken'],'refreshed')
    assert.deepEqual(JSON.parse(options.body),{consentement:true})
    return json({...analysis,status:'completed'})
  })
  assert.equal(await retryTreatment({...analysis,kind:'analysis'},()=>true),true)
})
test('import retry queues a server job with confirmation, without a continue call',async()=>{
  const calls=[]
  mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push(url)
    if(url==='/api/auth/csrf/')return json({})
    assert.equal(url,`/api/sync/imports/${id}/retry/`)
    assert.deepEqual(JSON.parse(options.body),{confirmer:true})
    return json({status:'queued'},202)
  })
  await retryTreatment({...job,kind:'import'},()=>true)
  assert.equal(calls.length,2)
})
test('quotas, CSRF and lost retry responses produce safe errors without automatic repeat',async()=>{
  for(const [status,code,pattern] of [[409,'retry_unavailable',/Relance indisponible/],[429,'quota_exceeded',/budget local/],[403,'csrf',/CSRF/]]){
    mock.method(globalThis,'fetch',async(url)=>url==='/api/auth/csrf/'?json({}):json({code,detail:'PRIVATE'},status))
    await assert.rejects(retryTreatment({...analysis,kind:'analysis'},()=>true),error=>pattern.test(error.message)&&!error.message.includes('PRIVATE'))
    mock.restoreAll()
  }
  let writes=0
  mock.method(globalThis,'fetch',async(url)=>{if(url==='/api/auth/csrf/')return json({});writes++;throw new TypeError('Network interrupted')})
  await assert.rejects(retryTreatment({...analysis,kind:'analysis'},()=>true))
  assert.equal(writes,1)
})
