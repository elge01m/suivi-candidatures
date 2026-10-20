import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRenderServer } from './render-server.mjs'

let server
let Form
let FilterControls
before(async () => {
  // Compile TSX with the project's existing Vite toolchain; no extra test library.
  server = await createRenderServer('candidatures')
  Form = (await server.ssrLoadModule('/src/CandidaturesPanel.tsx')).CandidatureForm
  FilterControls = (await server.ssrLoadModule('/src/CandidatureFilterControls.tsx')).default
})
after(async () => { await server?.close() })

function render(props = {}) {
  return renderToStaticMarkup(createElement(Form, {
    initial: 'new', busy: false, error: '', fields: {},
    onSubmit: async () => {}, onCancel: () => {}, ...props,
  }))
}

test('new form exposes only business fields and all eight French states and receipt choices', () => {
  const html = render()
  for (const name of ['employeur', 'poste', 'date_candidature', 'etat', 'confirmation_reception', 'lien', 'notes']) {
    assert.ok(html.includes(`name="${name}"`))
  }
  assert.equal((html.match(/<option /g) ?? []).length, 11)
  for (const label of ['Envoyée', 'Entrevue', 'Offre reçue', 'Offre acceptée', 'Offre déclinée', 'Refusée', 'Retirée']) {
    assert.ok(html.includes(label))
  }
  assert.doesNotMatch(html, /name="(?:utilisateur|id|date_creation|date_modification)"/)
  assert.match(html, /type="date"/)
  assert.match(html, /type="url"/)
  assert.match(html, /maxLength="200"/)
  assert.match(html, /facultatif/)
  assert.match(html, /Annuler/)
  assert.match(html, /À préparer/)
  assert.match(html, /Confirmation de réception/)
  assert.match(html, /value="" selected="">Non renseignée/)
})

test('preparation edit leaves the date optional and does not invent a receipt', () => {
  const html = render({ initial:{ id:7, employeur:'Fictif', poste:'Test', date_candidature:null,
    etat:'a_preparer', confirmation_reception:null, lien:'', notes:'' } })
  assert.match(html, /id="date_candidature" type="date"[^>]*name="date_candidature" value=""/)
  assert.doesNotMatch(html, /type="date" required/)
  assert.match(html, /value="" selected="">Non renseignée/)
})

test('edit form is populated and text is escaped safely', () => {
  const html = render({ initial: {
    id: 7, employeur: '<script>test</script>', poste: 'Poste test',
    date_candidature: '2026-10-18', etat: 'entrevue', confirmation_reception: null, lien: '', notes: 'Notes existantes',
  } })
  assert.match(html, /Modifier la candidature/)
  assert.match(html, /value="2026-10-18"/)
  assert.match(html, /value="entrevue" selected=""/)
  assert.match(html, /Notes existantes/)
  assert.doesNotMatch(html, /<script>/)
  assert.match(html, /&lt;script&gt;/)
})

test('pending form disables editing and shows its progress in French', () => {
  const html = render({ busy: true })
  assert.match(html, /aria-busy="true"/)
  assert.match(html, /<fieldset disabled=""/)
  assert.match(html, /Enregistrement…/)
})

test('validation feedback is linked to the affected field', () => {
  const html = render({ error: 'Vérifiez le formulaire.', fields: { employeur: 'Employeur requis.' } })
  assert.match(html, /role="alert"/)
  assert.match(html, /aria-invalid="true"/)
  assert.match(html, /aria-describedby="employeur-error"/)
  assert.match(html, /id="employeur-error"/)
  assert.match(html, /Employeur requis./)
})

test('filter controls provide a labelled search, all states and a reset button', () => {
  const html = renderToStaticMarkup(createElement(FilterControls, {
    criteria: { search: '', etat: '' }, disabled: false, onChange: () => {},
  }))
  assert.match(html, /type="search"/)
  assert.match(html, /for="candidature-search"/)
  assert.match(html, /for="candidature-state-filter"/)
  assert.match(html, /Tous les états/)
  assert.equal((html.match(/<option /g) ?? []).length, 9)
  assert.match(html, /disabled="">Réinitialiser les critères/)
})

test('active filters are displayed and reset is enabled', () => {
  const html = renderToStaticMarkup(createElement(FilterControls, {
    criteria: { search: 'Boréal', etat: 'entrevue' }, disabled: false, onChange: () => {},
  }))
  assert.match(html, /value="Boréal"/)
  assert.match(html, /value="entrevue" selected=""/)
  assert.doesNotMatch(html, /disabled=""/)
})
