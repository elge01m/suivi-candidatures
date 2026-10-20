import assert from 'node:assert/strict'
import { afterEach, beforeEach, mock, test } from 'node:test'
import {
  correctProposal, decideProposal, draftFrom, isDirty, listPendingProposals, parseProposal, validateDraft,
} from '../src/proposals.ts'
import { candidate, creation, id, json, proposal, secondId } from './proposal-fixtures.mjs'

beforeEach(() => { globalThis.document = { cookie: 'csrftoken=initial-token' } })
afterEach(() => { mock.restoreAll(); delete globalThis.document })

test('server association levels are parsed without ever selecting even a probable candidate', () => {
  for (const level of ['probable','a_confirmer','ambigu','aucune']) {
    const parsed = parseProposal({ ...proposal, proposition:{ ...proposal.proposition, niveau_association:level } })
    assert.equal(parsed.proposition.niveau_association, level)
    assert.equal(draftFrom(parsed).cible_id, '')
  }
  assert.throws(() => parseProposal({ ...proposal, proposition:{ ...proposal.proposition, niveau_association:'certain' } }))
})

test('legacy association metadata stays uncertain and explicit non-suggested target is allowed', () => {
  const old = structuredClone(proposal)
  delete old.proposition.niveau_association
  assert.equal(parseProposal(old).proposition.niveau_association, 'a_confirmer')
  const other = { ...candidate, id:8, employeur:'Autre entreprise', poste:'Autre poste' }
  const draft = { ...draftFrom(proposal), cible_id:'8' }
  assert.deepEqual(validateDraft(draft, [candidate,other], true), {})
  const saved = { ...proposal, proposition:{ ...proposal.proposition, cible_id:8 } }
  assert.equal(draftFrom(saved).cible_id, '8')
})

test('receipt proposal requires explicit target and value without requiring or selecting a state', () => {
  const receipt = { ...proposal, proposition:{ ...proposal.proposition, action:'confirmation_reception', etat:null, confirmation_reception:true } }
  const draft = draftFrom(receipt)
  assert.equal(draft.cible_id, '')
  assert.equal(draft.etat, '')
  assert.ok(validateDraft(draft, [candidate], true).cible_id)
  assert.deepEqual(validateDraft({ ...draft, cible_id:'7' }, [candidate], true), {})
  assert.ok(validateDraft({ ...draft, cible_id:'7', confirmation_reception:'' }, [candidate], true).confirmation_reception)
  assert.ok(validateDraft({ ...draft, cible_id:'7' }, [{ ...candidate, etat:'a_preparer', date_candidature:null }], true).confirmation_reception)
})

test('preparation proposals permit unknown dates, transitions require a date if the selected target has none', () => {
  const draft = { ...draftFrom(creation), etat:'a_preparer', date_candidature:'' }
  assert.deepEqual(validateDraft(draft, [], true), {})
  const transition = { ...draftFrom(proposal), cible_id:'7', etat:'envoyee' }
  assert.ok(validateDraft(transition, [{ ...candidate, etat:'a_preparer', date_candidature:null }], true).date_candidature)
  assert.deepEqual(validateDraft({ ...transition, date_candidature:'2026-10-13' }, [{ ...candidate, etat:'a_preparer', date_candidature:null }], true), {})
})

test('legacy proposals default receipt to unknown without altering the original object', () => {
  const old = structuredClone(proposal)
  delete old.proposition.confirmation_reception
  assert.equal(parseProposal(old).proposition.confirmation_reception, null)
  assert.equal(Object.hasOwn(old.proposition, 'confirmation_reception'), false)
  assert.throws(() => parseProposal({ ...proposal, proposition:{ ...proposal.proposition, confirmation_reception:'true' } }))
})

test('receipt correction submits only the saved human-selected target and tri-state value', async () => {
  const receipt = { ...proposal, proposition:{ ...proposal.proposition, action:'confirmation_reception', etat:null, confirmation_reception:true } }
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/auth/csrf/') return json({})
    assert.equal(options.method, 'PATCH')
    const p = JSON.parse(options.body).proposition
    assert.equal(p.cible_id, 7)
    assert.equal(p.etat, null)
    assert.equal(p.confirmation_reception, false)
    assert.equal(options.headers['X-CSRFToken'], 'initial-token')
    return json({ ...receipt, revision:2, proposition:{ ...receipt.proposition, cible_id:7, confirmation_reception:false } })
  })
  await correctProposal(receipt, { ...draftFrom(receipt), cible_id:'7', confirmation_reception:'false' }, [candidate])
})

test('unknown values remain empty and a unique candidate is never preselected', () => {
  const draft = draftFrom(proposal)
  assert.equal(draft.date_candidature, '')
  assert.equal(draft.cible_id, '')
  assert.ok(validateDraft(draft, [candidate], true).cible_id)
  const unknown = draftFrom({ ...proposal, proposition: { ...proposal.proposition, employeur: null, poste: null, etat: null } })
  assert.equal(unknown.employeur, '')
  assert.equal(unknown.poste, '')
  assert.equal(unknown.etat, '')
})

test('changing state requires explicit target, but does not require a missing application date', () => {
  const draft = { ...draftFrom(proposal), cible_id: '7' }
  assert.deepEqual(validateDraft(draft, [candidate], true), {})
  assert.ok(validateDraft(draft, [], true).cible_id)
  assert.ok(validateDraft({ ...draft, cible_id: '999' }, [candidate], true).cible_id)
})

test('creating requires employer, job, real date and known state without inventing defaults', () => {
  const draft = { action: 'creation', employeur: '', poste: '', date_candidature: '', etat: '', cible_id: '' }
  assert.deepEqual(Object.keys(validateDraft(draft, [], true)).sort(), ['employeur','poste','date_candidature','etat'].sort())
  assert.deepEqual(validateDraft(draft, []), {}) // Partial corrections can be saved.
  for (const date of ['2026-02-30','2026-13-01','0000-01-01','22/10/2026']) {
    assert.ok(validateDraft({ ...draftFrom(creation), date_candidature: date }, [], true).date_candidature)
  }
  assert.ok(validateDraft({ ...draftFrom(creation), etat: 'invented' }, [], true).etat)
})

test('dirty detection includes target, action and dates; no change triggers a write', () => {
  assert.equal(isDirty(proposal, draftFrom(proposal)), false)
  for (const change of [{ cible_id:'7' }, { action:'creation' }, { date_candidature:'2026-10-12' }, { employeur:'Correction' }]) {
    assert.equal(isDirty(proposal, { ...draftFrom(proposal), ...change }), true)
  }
})

test('list reads only pending details with session cookies, never analyses or Gmail', async () => {
  const calls = []
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url)
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.cache, 'no-store')
    assert.equal(options.method, undefined)
    if (url === '/api/propositions/') return json({ results: [
      { id, status:'en_attente', revision:1 }, { id:secondId, status:'acceptee', revision:1 },
    ] })
    assert.equal(url, `/api/propositions/${id}/`)
    return json(proposal)
  })
  assert.deepEqual(await listPendingProposals(), [proposal])
  assert.deepEqual(calls, ['/api/propositions/', `/api/propositions/${id}/`])
})

test('empty list and concurrent external decision are handled', async () => {
  mock.method(globalThis, 'fetch', async () => json({ results: [] }))
  assert.deepEqual(await listPendingProposals(), [])
  mock.restoreAll()
  mock.method(globalThis, 'fetch', async url => url === '/api/propositions/'
    ? json({ results: [{ id, status:'en_attente', revision:1 }] }) : json({ ...proposal, status:'rejetee' }))
  assert.deepEqual(await listPendingProposals(), [])
})

test('invalid JSON and malformed proposal payloads fail closed', async () => {
  for (const change of [{ revision:0 }, { id:'not-a-uuid' }, { proposition:{...proposal.proposition, etat:'unknown'} },
    { proposition:{...proposal.proposition, date_candidature:'2026-02-30'} }, { proposition:{...proposal.proposition, cible_id:-1} }]) {
    assert.throws(() => parseProposal({ ...proposal, ...change }))
  }
  mock.method(globalThis, 'fetch', async () => new Response('not JSON'))
  await assert.rejects(listPendingProposals(), /inattendue/)
})

test('metadata and detail IDs must match', async () => {
  mock.method(globalThis, 'fetch', async url => url === '/api/propositions/'
    ? json({ results: [{ id, status:'en_attente', revision:1 }] }) : json({ ...proposal, id:secondId }))
  await assert.rejects(listPendingProposals(), /inattendue/)
})

test('correction sends PATCH with full whitelisted data, null unknowns and revision, never acceptance', async () => {
  const calls = []
  const draft = { ...draftFrom(proposal), employeur:' ExempleTech corrigé ', cible_id:'7', utilisateur:999 }
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url)
    if (url === '/api/auth/csrf/') {
      document.cookie = 'csrftoken=rotated-token'
      return json({})
    }
    assert.equal(url, `/api/propositions/${id}/`)
    assert.equal(options.method, 'PATCH')
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.headers['X-CSRFToken'], 'rotated-token')
    assert.deepEqual(JSON.parse(options.body), { revision:1, proposition: {
      action:'changement_etat', employeur:'ExempleTech corrigé', poste:'Développeur logiciel',
      date_candidature:null, etat:'entrevue', confirmation_reception:null, cible_id:7,
    } })
    return json({ ...proposal, revision:2, proposition:{ ...proposal.proposition, employeur:'ExempleTech corrigé', cible_id:7, cible_version:'2026-10-19T10:00:00Z' } })
  })
  const result = await correctProposal(proposal, draft, [candidate])
  assert.equal(result.revision, 2)
  assert.equal(result.proposition.cible_id, 7)
  assert.deepEqual(calls, ['/api/auth/csrf/', `/api/propositions/${id}/`])
})

test('creation correction clears target and preserves unknown empty values as null', async () => {
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/auth/csrf/') return json({})
    const data = JSON.parse(options.body).proposition
    assert.equal(data.cible_id, null)
    assert.equal(data.employeur, null)
    assert.equal(data.date_candidature, null)
    assert.equal(data.etat, null)
    return json(proposal)
  })
  await correctProposal(proposal, { ...draftFrom(proposal), action:'creation', employeur:' ', etat:'', cible_id:'7' }, [candidate])
})

test('invalid corrections cause no HTTP call', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected request') })
  await assert.rejects(correctProposal(proposal, { ...draftFrom(proposal), employeur:'x'.repeat(201) }, []), /champs/)
  assert.equal(fetch.mock.callCount(), 0)
})

test('accepting without a target is refused locally before confirmation or HTTP', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected request') })
  let confirmed = false
  await assert.rejects(decideProposal(proposal, 'accept', () => { confirmed = true; return true }, [candidate]), /explicitement/)
  assert.equal(confirmed, false)
  assert.equal(fetch.mock.callCount(), 0)
})

test('cancelled acceptance and rejection cause no HTTP call', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected request') })
  assert.equal(await decideProposal(creation, 'accept', () => false), null)
  assert.equal(await decideProposal(proposal, 'reject', () => false), null)
  assert.equal(fetch.mock.callCount(), 0)
})

test('explicit acceptance submits saved revision and confirmation only, never writes CRUD directly', async () => {
  const saved = { ...proposal, revision:2, proposition:{...proposal.proposition, cible_id:7} }
  const calls = []
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(url)
    if (url === '/api/auth/csrf/') return json({})
    assert.equal(url, `/api/propositions/${id}/accept/`)
    assert.equal(options.method, 'POST')
    assert.equal(options.headers['X-CSRFToken'], 'initial-token')
    assert.deepEqual(JSON.parse(options.body), { revision:2, confirmer:true })
    return json({ ...saved, status:'acceptee', resultat_id:7 })
  })
  const result = await decideProposal(saved, 'accept', () => true, [candidate])
  assert.equal(result.status, 'acceptee')
  assert.equal(calls.length, 2)
})

test('reject submits explicit confirmation and does not require completing fields', async () => {
  mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/auth/csrf/') return json({})
    assert.equal(url, `/api/propositions/${id}/reject/`)
    assert.equal(options.method, 'POST')
    assert.equal(options.headers['X-CSRFToken'], 'initial-token')
    assert.deepEqual(JSON.parse(options.body), { revision:1, confirmer:true })
    return json({ ...proposal, status:'rejetee' })
  })
  assert.equal((await decideProposal(proposal, 'reject', () => true)).status, 'rejetee')
})

test('decided proposals cannot be submitted again', async () => {
  const fetch = mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected request') })
  await assert.rejects(decideProposal({ ...creation, status:'acceptee' }, 'accept', () => true), /déjà/)
  assert.equal(fetch.mock.callCount(), 0)
})

test('validation conflicts use safe French errors and never provider response content', async () => {
  for (const [code, status, expected] of [
    ['stale_revision',409,/relisez/], ['target_changed',409,/candidature a changé/],
    ['duplicate_creation',409,/existe déjà/], ['missing_fields',400,/obligatoires/], ['not_found',404,/introuvable/],
  ]) {
    mock.method(globalThis, 'fetch', async url => url === '/api/auth/csrf/' ? json({}) : json({ code, detail:'DO NOT DISPLAY' }, status))
    await assert.rejects(decideProposal(creation, 'accept', () => true), error => expected.test(error.message) && !error.message.includes('DO NOT DISPLAY'))
    mock.restoreAll()
  }
})

test('session, CSRF and unknown backend errors have safe messages', async () => {
  for (const status of [401,403,500]) {
    mock.method(globalThis, 'fetch', async () => json({ code:'unknown',detail:'PRIVATE' }, status))
    await assert.rejects(listPendingProposals(), error => !error.message.includes('PRIVATE'))
    mock.restoreAll()
  }
})

test('network failures never retry or repeat a decision automatically', async () => {
  let writes = 0
  mock.method(globalThis, 'fetch', async url => {
    if (url === '/api/auth/csrf/') return json({})
    writes += 1
    throw new TypeError('Fictional failure')
  })
  await assert.rejects(decideProposal(creation, 'accept', () => true), /injoignable/)
  assert.equal(writes, 1)
})

test('untrusted metadata is stripped and unsafe IDs cannot become routes', async () => {
  assert.equal(parseProposal({ ...proposal, utilisateur:999 }).utilisateur, undefined)
  await assert.rejects(correctProposal({ ...proposal,id:'../../analyses' }, draftFrom(proposal), []), /Identifiant/)
})
