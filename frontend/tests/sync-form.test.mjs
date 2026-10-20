import assert from 'node:assert/strict'
import { after,before,test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRenderServer } from './render-server.mjs'

let server, Form, Panel
const config={connected:true,gmail_enabled:false,auto_analysis_enabled:false,interval_minutes:5,timezone:'America/Toronto',revision:1,consent_at:null,next_due_at:null,last_success_at:null,blocked_code:'',analysis_pending:0,analyses_completed:0,analyses_failed:0,last_analysis_error:'',latest_run:null,latest_initial_import:null}
before(async()=>{
  server=await createRenderServer('sync')
  const module=await server.ssrLoadModule('/src/SyncPanel.tsx')
  Form=module.SyncForm
  Panel=module.default
})
after(async()=>{await server?.close()})

test('five-minute cadence, timezone and disabled default automation are visible',()=>{
  const html=renderToStaticMarkup(createElement(Form,{config,busy:false,onSave:async()=>{}}))
  assert.equal((html.match(/type="time"/g)??[]).length,0)
  assert.match(html,/America\/Toronto/)
  assert.match(html,/toutes les cinq minutes/)
  assert.doesNotMatch(html,/sync-first|sync-second/)
  assert.doesNotMatch(html,/checked=""/)
  assert.match(html,/n’applique jamais de proposition/)
  assert.match(html,/uniquement des courriels fictifs/)
})

test('recorded consent date and revocation instructions displayed',()=>{
  const html=renderToStaticMarkup(createElement(Form,{config:{...config,auto_analysis_enabled:true,consent_at:'2026-10-19T10:00:00Z'},busy:false,onSave:async()=>{}}))
  assert.match(html,/checked=""/)
  assert.match(html,/Consentement enregistré/)
  assert.match(html,/Pour le révoquer/)
  assert.match(html,/réponse sera écartée/)
})

test('busy form prevents duplicate edits',()=>{
  const html=renderToStaticMarkup(createElement(Form,{config,busy:true,onSave:async()=>{}}))
  assert.match(html,/aria-busy="true"/)
  assert.match(html,/<fieldset[^>]*disabled=""/)
  assert.match(html,/disabled="">Enregistrer les paramètres/)
})

test('initial panel does not trigger a mutation during render and explains server independence',()=>{
  const html=renderToStaticMarkup(createElement(Panel,{disabled:false,onPendingChange:()=>{},onUpdated:()=>{}}))
  assert.match(html,/Synchroniser maintenant/)
  assert.match(html,/disabled="">Synchroniser maintenant/)
  assert.match(html,/serveur permanent/)
  assert.match(html,/ordinateur local éteint/)
})
