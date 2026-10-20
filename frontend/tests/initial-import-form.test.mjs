import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRenderServer } from './render-server.mjs'

let server, Form, Panel, Progress
const period = { date_debut:'', date_fin:'', fuseau:'America/Toronto' }
before(async () => {
  server = await createRenderServer('initial-import')
  const module = await server.ssrLoadModule('/src/InitialImportPanel.tsx')
  Form = module.InitialImportForm; Panel = module.default; Progress = module.ImportProgress
})
after(async () => { await server?.close() })

test('initial form requires explicit inclusive dates and a timezone, never an owner', () => {
  const html = renderToStaticMarkup(createElement(Form,{period,busy:false,onChange:()=>{},onSubmit:()=>{}}))
  assert.equal((html.match(/type="date"/g) ?? []).length, 2)
  assert.match(html, /Date de début incluse/)
  assert.match(html, /Date de fin incluse/)
  assert.match(html, /America\/Toronto/)
  assert.match(html, /n’active pas l’analyse Gemini/)
  assert.doesNotMatch(html, /name="utilisateur"/)
})

test('rendering the panel never submits work or preselects a period', () => {
  let calls = 0
  const html = renderToStaticMarkup(createElement(Panel,{job:null,disabled:false,onPendingChange:()=>{calls++},onQueued:()=>{calls++}}))
  assert.equal(calls, 0)
  assert.match(html, /name="date_debut" value=""/)
  assert.match(html, /name="date_fin" value=""/)
})

test('busy form blocks duplicate submissions and editing', () => {
  let calls = 0
  const props = {period,busy:true,onChange:()=>{},onSubmit:()=>{calls++}}
  const html = renderToStaticMarkup(createElement(Form,props))
  assert.match(html, /disabled=""/)
  Form(props).props.onSubmit({preventDefault:()=>{}})
  assert.equal(calls, 0)
})

test('progress shows safe counters, incomplete texts and deferred retry, without percentages', () => {
  const job = {id:'fiction', origin:'initial', status:'paused',date_debut:'2026-10-12',date_fin:'2026-10-12',fuseau:'America/Toronto',
    processed:12,imported:9,duplicates:2,skipped:1,incomplete:1,error_code:'rate_limited',retry_after:'2026-10-19T19:00:00Z'}
  const html = renderToStaticMarkup(createElement(Progress,{job}))
  assert.match(html,/En pause/)
  assert.match(html,/12 examinés/)
  assert.match(html,/9 importés/)
  assert.match(html,/2 doublons évités/)
  assert.match(html,/1 contenus incomplets/)
  assert.match(html,/lots de dix messages/)
  assert.match(html,/Reprise possible/)
  assert.doesNotMatch(html,/%/)
})
