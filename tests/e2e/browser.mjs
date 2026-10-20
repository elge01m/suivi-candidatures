// Real installed Chromium via CDP and Node's built-in WebSocket. No DOM mocks.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import path from 'node:path'

const origin = 'http://127.0.0.1:15173'
const root = process.cwd()
const checks = [], responses = [], errors = []
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
let socket, sequence = 0, targetId
let acceptDialog = true, dialogs = 0
const pending = new Map()
async function command(method, params = {}) {
  const id = ++sequence
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(Error(`CDP timeout: ${method}`)) }, 15000)
    pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value) }, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })
}
async function open() {
  const target = await (await fetch('http://127.0.0.1:19222/json/new?about:blank', { method: 'PUT' })).json()
  targetId = target.id
  socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject })
  socket.onmessage = event => {
    const item = JSON.parse(event.data)
    if (item.id) {
      const job = pending.get(item.id)
      if (job) { pending.delete(item.id); item.error ? job.reject(Error(item.error.message)) : job.resolve(item.result) }
    } else if (item.method === 'Page.javascriptDialogOpening') {
      dialogs++
      void command('Page.handleJavaScriptDialog', { accept: acceptDialog })
    } else if (item.method === 'Runtime.exceptionThrown') {
      errors.push('Uncaught browser exception')
    } else if (item.method === 'Network.responseReceived') {
      const r = item.params.response
      if (r.url.startsWith(origin + '/api/')) responses.push({ path: new URL(r.url).pathname, status: r.status })
    }
  }
  // A fresh target can expose its WebSocket before Chrome attaches its renderer.
  for (let attempt=0; ; attempt++) {
    try { await command('Page.enable'); break }
    catch(error) { if (attempt>=30 || !error.message.includes('Not attached')) throw error; await pause(100) }
  }
  await command('Runtime.enable'); await command('Network.enable')
  await command('Network.setBlockedURLs', { urls: ['*://*.googleapis.com/*', '*://accounts.google.com/*', '*://*.google.com/*'] })
  await command('Page.navigate', { url: origin })
  await wait(`location.origin===${JSON.stringify(origin)} && document.readyState==='complete'`, 'new browser page ready')
}
async function evaluate(expression) {
  const r = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw Error('Browser evaluation failed: ' + expression.slice(0, 100))
  return r.result.value
}
async function wait(expression, label = expression) {
  const deadline = Date.now() + 15000
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return
    await pause(100)
  }
  throw Error('Timeout: ' + label)
}
const q = selector => `document.querySelector(${JSON.stringify(selector)})`
async function click(selector) {
  await wait(`${q(selector)} && !${q(selector)}.matches(':disabled')`, 'enabled ' + selector)
  const box = await evaluate(`(()=>{const e=${q(selector)};e.scrollIntoView({block:'center'});const b=e.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2}})()`)
  await command('Input.dispatchMouseEvent', { type:'mousePressed', button:'left', clickCount:1, ...box })
  await command('Input.dispatchMouseEvent', { type:'mouseReleased', button:'left', clickCount:1, ...box })
}
async function button(text, scope = 'document') {
  const expression = `Array.from(${scope}.querySelectorAll('button')).find(e=>e.textContent.trim()===${JSON.stringify(text)})`
  await wait(`!!(${expression}) && !(${expression}).disabled`, 'button ' + text)
  // Give the actual element a temporary locator, then dispatch real mouse events.
  await evaluate(`(${expression}).setAttribute('data-e2e-button','current')`)
  await click('[data-e2e-button="current"]')
  await evaluate(`document.querySelector('[data-e2e-button="current"]')?.removeAttribute('data-e2e-button')`)
}
async function fill(selector, value) {
  await wait(`!!${q(selector)} && !${q(selector)}.matches(':disabled')`)
  await evaluate(`(()=>{const e=${q(selector)};const p=e.tagName==='SELECT'?HTMLSelectElement.prototype:e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`)
}
async function checkbox(index, checked) {
  const e=`document.querySelectorAll('form[aria-label="Paramètres de synchronisation"] input[type="checkbox"]')[${index}]`
  await wait(`!!(${e}) && !(${e}).matches(':disabled')`, 'ready sync checkbox '+index)
  if (await evaluate(`(${e}).checked!==${checked}`)) {
    await evaluate(`(${e}).setAttribute('data-e2e-check','current')`)
    await click('[data-e2e-check="current"]')
    await evaluate(`document.querySelector('[data-e2e-check="current"]')?.removeAttribute('data-e2e-check')`)
  }
  await wait(`(${e})?.checked===${checked}`,'sync checkbox changed '+index)
}
async function api(url, method = 'GET', data, csrf = true) {
  return evaluate(`(async()=>{const token=document.cookie.split('; ').find(c=>c.startsWith('csrftoken='))?.split('=')[1];const r=await fetch(${JSON.stringify(url)},{method:${JSON.stringify(method)},credentials:'same-origin',headers:{'Content-Type':'application/json',...(${csrf}&&token?{'X-CSRFToken':decodeURIComponent(token)}:{})},...(${data !== undefined}?{body:JSON.stringify(${JSON.stringify(data ?? null)})}:{})});return {status:r.status,data:await r.json().catch(()=>null)}})()`)
}
function control(action) {
  const r = spawnSync(path.join(root,'backend/.venv/Scripts/python.exe'), ['-B','-X','utf8',path.join(root,'tests/e2e/backend_control.py'),action], { env:process.env, encoding:'utf8', windowsHide:true })
  assert.equal(r.status, 0, 'Isolated control failed: ' + action + '\n' + r.stderr)
  return action === 'snapshot' ? JSON.parse(r.stdout.trim()) : r.stdout.trim()
}
function providers(data) { writeFileSync(path.join(process.env.E2E_TEMP,'provider.json'),JSON.stringify(data)) }
async function test(name, fn) { await fn(); checks.push({ name, passed:true }); console.log('OK ' + name) }
const cardScope = `Array.from(document.querySelectorAll('.candidatures .application-card')).find(e=>e.textContent.includes('Fiction E2E'))`
async function saved() { await wait(`!document.querySelector('#employeur') && !!document.querySelector('.candidatures .application-card')`, 'form saved') }
async function login() {
  await wait(`!!document.querySelector('#username')`)
  await fill('#username','browser-fiction'); await fill('#password','fiction-only-test-password')
  await button('Se connecter')
  await wait(`document.querySelector('#page-title')?.textContent==='Mes candidatures'`)
  await wait(`!!document.querySelector('.proposals .proposal-card')`)
  await wait(`document.querySelector('.candidatures')?.getAttribute('aria-busy')==='false'`)
}
async function refresh() {
  await command('Page.reload'); await wait(`document.querySelector('#page-title')?.textContent==='Mes candidatures'`)
  await wait(`document.querySelector('.candidatures')?.getAttribute('aria-busy')==='false'`)
}
try {
  await open()
  await test('Session login, CSRF and authenticated API', async()=>{
    await login()
    assert.equal((await api('/api/candidatures/','POST',{},false)).status,403)
    assert.equal((await api('/api/auth/me/')).status,200)
  })
  await test('Create preparation without date or invented receipt',async()=>{
    await button('Ajouter une candidature')
    await fill('#employeur','Fiction E2E'); await fill('#poste','Logiciel fictif')
    await fill('#etat','a_preparer'); await fill('#date_candidature','')
    await button('Enregistrer'); await saved()
    const c=control('snapshot').candidates[0]
    assert.equal(c.etat,'a_preparer'); assert.equal(c.date_candidature,null); assert.equal(c.confirmation_reception,null)
  })
  await test('Edit through all eight states and independent receipt',async()=>{
    for (const state of ['envoyee','entrevue','offre_recue','offre_acceptee','offre_declinee','refusee','retiree','a_preparer']) {
      await button('Modifier',cardScope)
      await fill('#date_candidature','2026-10-13'); await fill('#etat',state)
      await fill('#notes','Notes fictives conservées')
      await fill('#confirmation_reception',state==='entrevue'?'true':'')
      await button('Enregistrer'); await saved()
      assert.equal((await api('/api/candidatures/')).data[0].etat,state)
    }
    await button('Modifier',cardScope);await fill('#etat','envoyee');await fill('#confirmation_reception','true')
    await button('Enregistrer');await saved()
    assert.equal(control('snapshot').candidates[0].confirmation_reception,true)
  })
  await test('Search, state combination, empty result and reset',async()=>{
    await fill('#candidature-search','FICTION');await fill('#candidature-state-filter','envoyee')
    assert.equal(await evaluate(`document.querySelectorAll('.candidatures .application-card').length`),1)
    await fill('#candidature-state-filter','refusee')
    await wait(`document.querySelectorAll('.candidatures .application-card').length===0`)
    await button('Réinitialiser les critères');await wait(`document.querySelectorAll('.candidatures .application-card').length===1`)
    await fill('#candidature-search','LOGICIEL');assert.equal(await evaluate(`document.querySelectorAll('.candidatures .application-card').length`),1)
    await button('Réinitialiser les critères')
  })
  await test('Proposal correction, explicit target and confirmed acceptance',async()=>{
    await button('Actualiser les propositions');await button('Examiner la proposition')
    const selector='select[name="cible_id"]'
    await wait(`!!${q(selector)}`);assert.equal(await evaluate(`${q(selector)}.value`),'')
    assert.equal(await evaluate(`Array.from(document.querySelectorAll('button')).find(e=>e.textContent==='Accepter la proposition…').disabled`),true)
    const candidate=control('snapshot').candidates[0]
    await fill('input[name="poste"]','Logiciel fictif corrigé')
    await fill(selector,String(candidate.id));await button('Enregistrer les corrections')
    await wait(`document.querySelector('.proposals')?.getAttribute('aria-busy')==='false'`)
    assert.equal(control('snapshot').candidates[0].etat,'envoyee')
    await button('Accepter la proposition…')
    await wait(`!document.querySelector('form[aria-label="Corriger et décider"]')`)
    assert.equal(control('snapshot').candidates[0].etat,'entrevue')
    assert.equal(control('snapshot').candidates[0].date_candidature,'2026-10-13')
    assert.equal(control('snapshot').candidates[0].poste,'Logiciel fictif')
    const accepted=control('snapshot').proposals.find(p=>p.status==='acceptee')
    const detail=await api(`/api/propositions/${accepted.id}/`)
    const repeated=await api(`/api/propositions/${accepted.id}/accept/`,'POST',{revision:detail.data.revision,confirmer:true})
    assert.equal(repeated.status,200)
    assert.equal(control('snapshot').candidates.length,1)
  })
  await test('Proposal rejection, history and persistence',async()=>{
    await button('Examiner la proposition');await button('Rejeter la proposition…')
    await wait(`!document.querySelector('form[aria-label="Corriger et décider"]')`)
    await refresh();await button('Consulter l’historique',cardScope)
    await wait(`!!document.querySelector('.history-table')`)
    const s=control('snapshot');assert.equal(s.candidates.length,1)
    assert.equal(s.proposals.filter(p=>p.status==='acceptee').length,1)
    assert.equal(s.proposals.filter(p=>p.status==='rejetee').length,1)
    assert.equal(s.candidates[0].etat,'entrevue');assert.equal(s.candidates[0].notes,'Notes fictives conservées')
  })
  await test('Analysis error and explicitly consented simulated retry',async()=>{
    await button('Actualiser les traitements')
    await button('Réessayer avec un nouveau consentement…')
    await wait(`document.body.textContent.includes('Nouvelle tentative terminée.')`)
    const s=control('snapshot');assert.equal(s.analyses,3);assert.equal(s.candidates.length,1)
    assert.equal(s.candidates[0].etat,'entrevue')
  })
  await test('Two-user isolation through real authenticated HTTP requests',async()=>{
    const s=control('snapshot'), id=s.candidates[0].id, proposal=s.proposals[0].id
    assert.equal((await api('/api/auth/logout/','POST',{})).status,204)
    assert.equal((await api('/api/auth/login/','POST',{username:'other-fiction',password:'fiction-only-test-password'})).status,200)
    assert.deepEqual((await api('/api/candidatures/')).data,[])
    assert.equal((await api(`/api/candidatures/${id}/`)).status,404)
    assert.equal((await api(`/api/candidatures/${id}/`,'PATCH',{etat:'refusee'})).status,404)
    assert.equal((await api(`/api/candidatures/${id}/historique/`)).status,404)
    assert.equal((await api(`/api/propositions/${proposal}/`)).status,404)
    await api('/api/auth/logout/','POST',{});await command('Page.reload');await login()
  })
  await test('Mobile viewport without horizontal overflow',async()=>{
    await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true})
    assert(await evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
    await command('Emulation.clearDeviceMetricsOverride')
  })
  const stamp=new Date().toISOString();const day=new Intl.DateTimeFormat('en-CA',{timeZone:'America/Toronto',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())
  await test('Initial import HTTP 202 and queued server work',async()=>{
    providers({message_time:stamp})
    await fill('#import-start',day);await fill('#import-end',day);await button('Importer la période…')
    await wait(`document.body.textContent.includes('Demande enregistrée.')`)
    assert(responses.some(r=>r.path==='/api/sync/imports/'&&r.status===202))
    assert.equal(control('snapshot').runs.at(-1).status,'queued')
    assert.equal(control('snapshot').messages,3)
  })
  await test('Browser closed: pagination, bounded batches and restart resume',async()=>{
    await fetch(`http://127.0.0.1:19222/json/close/${targetId}`);socket.close()
    control('worker')
    let s=control('snapshot');assert.equal(s.runs.at(-1).import_job__processed,10);assert.notEqual(s.runs.at(-1).status,'completed')
    // A new OS process invokes the same production command and resumes its cursor.
    control('worker');s=control('snapshot');assert.equal(s.runs.at(-1).status,'completed')
    assert.equal(s.runs.at(-1).import_job__imported,12);assert.equal(s.messages,15);assert(s.encrypted)
    await open();await refresh()
    await button('Actualiser le suivi');await wait(`document.body.textContent.includes('12 importés')`)
  })
  await test('Manual synchronization, errors and controlled UI retry',async()=>{
    providers({fail:true,message_time:stamp})
    await button('Synchroniser maintenant');await wait(`document.body.textContent.includes('Synchronisation enregistrée.')`)
    assert(responses.some(r=>r.path==='/api/sync/runs/'&&r.status===202))
    control('worker');assert.equal(control('snapshot').runs.at(-1).status,'paused')
    control('retry-ready');await button('Actualiser les traitements')
    await button('Remettre l’import en attente…')
    providers({message_time:stamp});control('worker');control('worker')
    const s=control('snapshot');assert.equal(s.runs.at(-1).status,'completed')
    assert.equal(s.runs.at(-1).import_job__duplicates,12);assert.equal(s.messages,15)
  })
  await test('Explicit automatic AI consent, new messages only, no automatic application',async()=>{
    await button('Actualiser le suivi')
    await checkbox(0,true);await checkbox(1,true);await checkbox(2,true)
    await button('Enregistrer les paramètres')
    await wait(`document.body.textContent.includes('Consentement enregistré le')`)
    providers({prefix:'new-consented',message_time:new Date().toISOString()})
    await button('Synchroniser maintenant');await wait(`document.body.textContent.includes('Synchronisation enregistrée.')`)
    await fetch(`http://127.0.0.1:19222/json/close/${targetId}`);socket.close()
    control('worker');control('worker');control('budget-ready');control('worker')
    const s=control('snapshot');assert.equal(s.completed_automatic,1);assert.equal(s.automatic_analyses,12)
    assert.equal(s.candidates.length,1);assert.equal(s.candidates[0].etat,'entrevue');assert.equal(s.messages,27)
    assert.equal(s.proposals.filter(p=>p.status==='acceptee').length,1)
    await open();await refresh();await button('Actualiser le suivi')
    await checkbox(1,false);await checkbox(0,false)
    await button('Enregistrer les paramètres')
    await wait(`!document.body.textContent.includes('Consentement enregistré le')`)
    assert.equal(control('snapshot').auto_analysis_enabled,false);assert.equal(control('snapshot').gmail_enabled,false)
  })
  await test('Schedule with fake providers, browser independence and disabled AI',async()=>{
    await button('Actualiser le suivi')
    await checkbox(0,true)
    await button('Enregistrer les paramètres');await wait(`document.body.textContent.includes('Paramètres enregistrés')`)
    control('schedule-due')
    await fetch(`http://127.0.0.1:19222/json/close/${targetId}`);socket.close()
    control('worker');control('worker')
    const s=control('snapshot');assert.equal(s.runs.at(-1).origin,'scheduled');assert.equal(s.runs.at(-1).status,'completed')
    assert.equal(s.completed_automatic,1);assert.equal(s.auto_analysis_enabled,false);assert.equal(s.unique_messages,s.messages)
    await open();await refresh();await button('Actualiser le suivi')
    await checkbox(0,false);await button('Enregistrer les paramètres')
    await wait(`!document.querySelector('form[aria-label="Paramètres de synchronisation"] input[type="checkbox"]').checked`)
  })
  await test('Delete with confirmation and logout',async()=>{
    acceptDialog=false
    await button('Supprimer',cardScope);await pause(150)
    assert.equal(control('snapshot').candidates.length,1)
    acceptDialog=true
    await button('Supprimer',cardScope);await wait(`document.querySelectorAll('.candidatures .application-card').length===0`)
    assert.equal(control('snapshot').candidates.length,0)
    await button('Déconnexion');await wait(`!!document.querySelector('#username')`)
    assert.equal((await api('/api/candidatures/')).status,403)
  })
  assert.equal(errors.length,0,'Uncaught JavaScript exceptions')
  assert.equal(responses.filter(r=>r.status>=500).length,0,'Unexpected server HTTP errors')
  // Accept, reject, analysis retry, initial import, import retry, cancel delete, confirm delete.
  assert.equal(dialogs,7,'All seven expected confirmation dialogs actually exercised')
} catch(error) {
  checks.push({name:'execution',passed:false,error:error.message,stack:error.stack})
  console.error(error.stack)
  process.exitCode=1
} finally {
  const report={real_browser:true,providers:'simulated',application_database:'isolated_postgresql',checks,responses,confirmation_dialogs:dialogs,uncaught_errors:errors.length}
  writeFileSync(path.join(process.env.E2E_TEMP,'browser-report.json'),JSON.stringify(report,null,2))
  socket?.close()
}
