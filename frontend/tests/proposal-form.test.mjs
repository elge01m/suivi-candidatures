import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRenderServer } from './render-server.mjs'
import { draftFrom } from '../src/proposals.ts'
import { candidate, creation, proposal } from './proposal-fixtures.mjs'

let server
let Form
before(async () => {
  server = await createRenderServer('proposals')
  Form = (await server.ssrLoadModule('/src/ProposalsPanel.tsx')).ProposalForm
})
after(async () => { await server?.close() })

function render(overrides = {}) {
  return renderToStaticMarkup(createElement(Form, {
    proposal, draft:draftFrom(proposal), candidates:[candidate], busy:false, error:'', errors:{},
    onChange:() => {}, onSave:() => {}, onAccept:() => {}, onReject:() => {}, onCancel:() => {}, ...overrides,
  }))
}

test('target placeholder is selected even with a sole existing match', () => {
  const html = render()
  assert.match(html, /value="" selected="">Sélectionnez vous-même/)
  assert.doesNotMatch(html, /value="7" selected/)
  assert.match(html, /cible_id/)
  assert.match(html, /choix obligatoire/)
  assert.match(html, /disabled="">Accepter la proposition/)
  assert.match(html, /Sélectionnez explicitement/)
})

test('probable server suggestion is visible and other candidates stay selectable with no default target', () => {
  const other = { ...candidate, id:8, employeur:'Autre entreprise', poste:'Autre poste' }
  const html = render({ candidates:[candidate,other] })
  assert.match(html, /Correspondance probable/)
  assert.match(html, /Suggestions du serveur/)
  assert.match(html, /Autres candidatures/)
  assert.match(html, /Autre entreprise/)
  assert.match(html, /value="" selected="">Sélectionnez vous-même/)
  assert.doesNotMatch(html, /value="[78]" selected/)
})

test('ambiguous, partial and missing suggestions are labelled without automatic association', () => {
  for (const [level,label] of [['ambigu',/Correspondances ambiguës/], ['a_confirmer',/Correspondance partielle à confirmer/], ['aucune',/Aucune correspondance suggérée/]]) {
    const current = { ...proposal, proposition:{ ...proposal.proposition, niveau_association:level } }
    const html = render({ proposal:current, draft:draftFrom(current) })
    assert.match(html, label)
    assert.doesNotMatch(html, /value="7" selected/)
  }
})

test('server suggestion outside the available user candidates exposes no identifier or label', () => {
  const current = { ...proposal, proposition:{ ...proposal.proposition, candidatures_possibles:[999] } }
  const html = render({ proposal:current, draft:draftFrom(current), candidates:[candidate] })
  assert.match(html, /ne sont plus disponibles/)
  assert.doesNotMatch(html, /999/)
  assert.match(html, /Autres candidatures/)
})

test('unknown date and fields stay empty without default date or state', () => {
  const unknown = { ...proposal, proposition:{...proposal.proposition, employeur:null,poste:null,etat:null} }
  const html = render({ proposal:unknown, draft:draftFrom(unknown) })
  assert.match(html, /name="date_candidature" value=""/)
  assert.match(html, /value="" selected="">Non renseigné/)
  assert.match(html, /Ne saisissez pas la date d’entrevue/)
  assert.doesNotMatch(html, /name="(?:utilisateur|encrypted_data|cible_version|revision)"/)
})

test('eight French state options and four action choices are available', () => {
  const html = render()
  for (const label of ['Envoyée','Entrevue','Offre reçue','Offre acceptée','Offre déclinée','Refusée','Retirée',
    'À préparer','Confirmer la réception','Créer une candidature','Changer un état','Aucune action']) assert.ok(html.includes(label))
})

test('receipt review preserves the state, requires target and remains human-confirmed', () => {
  const receipt = { ...proposal, proposition:{ ...proposal.proposition, action:'confirmation_reception', etat:null, confirmation_reception:true } }
  const html = render({ proposal:receipt, draft:draftFrom(receipt) })
  assert.match(html, /name="etat" disabled=""/)
  assert.match(html, /value="" selected="">Sélectionnez vous-même/)
  assert.match(html, /l’état et la date seront conservés/)
  assert.match(html, /disabled="">Accepter la proposition/)
})

test('unsaved correction cannot be accepted; saving has its own submit action', () => {
  const html = render({ proposal:creation, draft:{...draftFrom(creation), employeur:'Correction'} })
  assert.match(html, /Corrections non enregistrées/)
  assert.match(html, /type="submit"[^>]*>Enregistrer les corrections/)
  assert.match(html, /type="button" disabled="">Accepter la proposition/)
  assert.match(html, /type="button"[^>]*>Rejeter la proposition/)
})

test('complete saved creation is ready for an explicit confirmation', () => {
  const html = render({ proposal:creation, draft:draftFrom(creation) })
  assert.doesNotMatch(html, /disabled="">Accepter la proposition/)
  assert.doesNotMatch(html, /name="cible_id"/)
})

test('missing candidates explain how to correct action without creating anything', () => {
  const html = render({ candidates:[] })
  assert.match(html, /Aucune candidature existante/)
  assert.match(html, /corriger l’action/)
  assert.match(html, /L’état et une confirmation explicitement renseignée seront modifiés/)
})

test('busy status prevents edits and duplicate clicks', () => {
  const html = render({ busy:true })
  assert.match(html, /aria-busy="true"/)
  assert.match(html, /<fieldset disabled=""/)
  assert.match(html, /Traitement en cours/)
  assert.match(html, /disabled="">Rejeter la proposition/)
})

test('validation and backend errors are visible and tied to fields', () => {
  const html = render({ error:'La candidature a changé.', errors:{cible_id:'Choisissez une cible.'} })
  assert.match(html, /role="alert"/)
  assert.match(html, /La candidature a changé/)
  assert.match(html, /aria-invalid="true"/)
  assert.match(html, /aria-describedby="proposal-[^"]+-cible_id-error"/)
  assert.match(html, /Choisissez une cible/)
})

test('model text is rendered as escaped text', () => {
  const html = render({ draft:{...draftFrom(proposal), employeur:'<script>fiction</script>'} })
  assert.doesNotMatch(html, /<script>/)
  assert.match(html, /&lt;script&gt;/)
})

function findElement(tree, predicate) {
  if (Array.isArray(tree)) {
    for (const child of tree) {
      const result = findElement(child, predicate)
      if (result) return result
    }
  } else if (tree && typeof tree === 'object' && tree.props) {
    if (predicate(tree)) return tree
    return findElement(tree.props.children, predicate)
  }
  return undefined
}

function formProps(overrides = {}) {
  return { proposal, draft:draftFrom(proposal), candidates:[candidate], busy:false, error:'', errors:{},
    onChange:() => {}, onSave:() => {}, onAccept:() => {}, onReject:() => {}, onCancel:() => {}, ...overrides }
}

test('changing action explicitly clears a previously chosen target', () => {
  let changed
  const saved = { ...proposal, proposition:{ ...proposal.proposition,cible_id:7 } }
  const tree = Form(formProps({ proposal:saved, draft:draftFrom(saved), onChange:value => { changed = value } }))
  findElement(tree, el => el.type === 'select' && el.props.name === 'action').props.onChange({ target:{value:'creation'} })
  assert.equal(changed.action, 'creation')
  assert.equal(changed.cible_id, '')
})

test('editing and saving never invoke acceptance; accepting needs a separate saved review', () => {
  let saves = 0
  let accepts = 0
  let draft = draftFrom(creation)
  const base = formProps({ proposal:creation, onChange:value => { draft = value }, onSave:() => { saves += 1 }, onAccept:() => { accepts += 1 } })
  let tree = Form({ ...base, draft })
  findElement(tree, el => el.type === 'input' && el.props.name === 'employeur').props.onChange({target:{value:'Correction fictive'}})
  assert.equal(saves, 0)
  assert.equal(accepts, 0)
  tree = Form({ ...base, draft })
  const acceptBeforeSave = findElement(tree, el => el.type === 'button' && el.props.children === 'Accepter la proposition…')
  assert.equal(acceptBeforeSave.props.disabled, true)
  tree.props.onSubmit({preventDefault:() => {}})
  assert.equal(saves, 1)
  assert.equal(accepts, 0)
  const saved = { ...creation, revision:2, proposition:{ ...creation.proposition, employeur:draft.employeur } }
  tree = Form({ ...base, proposal:saved, draft:draftFrom(saved) })
  const acceptAfterSave = findElement(tree, el => el.type === 'button' && el.props.children === 'Accepter la proposition…')
  assert.equal(acceptAfterSave.props.disabled, false)
  assert.equal(accepts, 0)
  acceptAfterSave.props.onClick()
  assert.equal(accepts, 1)
})

test('reject interaction is independent from correction and acceptance', () => {
  let rejects = 0
  let saves = 0
  let accepts = 0
  const tree = Form(formProps({ onReject:() => { rejects += 1 }, onSave:() => { saves += 1 }, onAccept:() => { accepts += 1 } }))
  findElement(tree, el => el.type === 'button' && el.props.children === 'Rejeter la proposition…').props.onClick()
  assert.deepEqual([rejects,saves,accepts], [1,0,0])
})
