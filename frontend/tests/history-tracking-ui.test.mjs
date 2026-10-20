import assert from 'node:assert/strict'
import { after,before,test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRenderServer } from './render-server.mjs'

let server,History,Entries,Rows,Tracking
before(async()=>{
  server=await createRenderServer('history-tracking')
  const history=await server.ssrLoadModule('/src/HistoryPanel.tsx')
  History=history.default;Entries=history.HistoryEntries
  const tracking=await server.ssrLoadModule('/src/TrackingPanel.tsx')
  Tracking=tracking.default;Rows=tracking.TreatmentRows
})
after(async()=>{await server?.close()})
test('history starts closed and does not read or change anything during render',()=>{
  const html=renderToStaticMarkup(createElement(History,{id:7,version:1}))
  assert.match(html,/Consulter l’historique/)
  assert.match(html,/aria-expanded="false"/)
  assert.doesNotMatch(html,/<table/)
})
test('empty history explains that past changes are not invented',()=>{
  const html=renderToStaticMarkup(createElement(Entries,{entries:[]}))
  assert.match(html,/ne sont pas reconstitués/)
})
test('history shows before and after, French states and surviving mail reference with escaped values',()=>{
  const html=renderToStaticMarkup(createElement(Entries,{entries:[{id:1,date:'2026-10-19T19:00:00Z',origine:'proposition',
    avant:{etat:'envoyee'},apres:{etat:'entrevue',notes:'<script>Fiction</script>'},source_message_reference:5,source_message_id:null,source_proposition_reference:null}]}))
  assert.match(html,/Avant/);assert.match(html,/Après/)
  assert.match(html,/Envoyée/);assert.match(html,/Entrevue/)
  assert.match(html,/source supprimée/)
  assert.doesNotMatch(html,/<script>/)
})
test('treatment errors are safe and retry button depends on server eligibility',()=>{
  const row={id:'fiction',kind:'analysis',message_reference:5,status:'failed',error_code:'PRIVATE',retry_after:null,attempts:3,can_retry:false}
  let html=renderToStaticMarkup(createElement(Rows,{rows:[row],busy:false,onRetry:()=>{}}))
  assert.match(html,/Relance indisponible/)
  assert.doesNotMatch(html,/PRIVATE|nouveau consentement/)
  html=renderToStaticMarkup(createElement(Rows,{rows:[{...row,can_retry:true}],busy:true,onRetry:()=>{}}))
  assert.match(html,/disabled=""/)
  assert.match(html,/nouveau consentement/)
})
test('tracking rendering never retries or enables an automatic treatment',()=>{
  let calls=0
  const html=renderToStaticMarkup(createElement(Tracking,{disabled:false,onPendingChange:()=>calls++,onUpdated:()=>calls++}))
  assert.equal(calls,0)
  assert.match(html,/consentement explicite/)
})
