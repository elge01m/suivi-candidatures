import assert from 'node:assert/strict'
import { test } from 'node:test'
import { filterCandidatures } from '../src/candidatureFilters.ts'
import { ETATS } from '../src/candidatures.ts'

const items = [
  { id: 3, employeur: 'Atelier Boréal', poste: 'Développeuse React', date_candidature: '2026-10-18', etat: 'entrevue', lien: '', notes: '' },
  { id: 2, employeur: 'Nord Conseil', poste: 'Analyste', date_candidature: '2026-10-15', etat: 'envoyee', lien: '', notes: '' },
  { id: 1, employeur: 'Atelier Boréal', poste: 'Analyste React', date_candidature: '2026-10-01', etat: 'envoyee', lien: '', notes: 'Texte confidentiel' },
]
const ids = (result) => result.map((item) => item.id)

test('no criteria returns the complete list in its existing descending date order', () => {
  assert.deepEqual(ids(filterCandidatures(items, { search: '', etat: '' })), [3, 2, 1])
})

test('employer search matches substrings regardless of case', () => {
  assert.deepEqual(ids(filterCandidatures(items, { search: 'ATELIER BORÉAL', etat: '' })), [3, 1])
  assert.deepEqual(ids(filterCandidatures(items, { search: 'conseil', etat: '' })), [2])
})

test('job title search matches substrings regardless of case', () => {
  assert.deepEqual(ids(filterCandidatures(items, { search: 'react', etat: '' })), [3, 1])
  assert.deepEqual(ids(filterCandidatures(items, { search: 'DÉVELOPPEUSE', etat: '' })), [3])
})

test('leading and trailing search whitespace is ignored', () => {
  assert.deepEqual(ids(filterCandidatures(items, { search: '  analyste  ', etat: '' })), [2, 1])
  assert.deepEqual(ids(filterCandidatures(items, { search: '   ', etat: '' })), [3, 2, 1])
})

test('equivalent composed and decomposed accents match', () => {
  assert.deepEqual(ids(filterCandidatures(items, { search: 'BORE\u0301AL', etat: '' })), [3, 1])
})

test('search does not inspect notes or unrelated fields', () => {
  assert.deepEqual(filterCandidatures(items, { search: 'confidentiel', etat: '' }), [])
  assert.deepEqual(filterCandidatures(items, { search: '2026', etat: '' }), [])
})

test('each of the eight states can be selected independently', () => {
  const allStates = ETATS.map((state, index) => ({ ...items[0], id: index + 1, etat: state.value }))
  for (const [index, state] of ETATS.entries()) {
    assert.deepEqual(ids(filterCandidatures(allStates, { search: '', etat: state.value })), [index + 1])
  }
})

test('search and state are combined with AND while employer and title use OR', () => {
  assert.deepEqual(ids(filterCandidatures(items, { search: 'react', etat: 'envoyee' })), [1])
  assert.deepEqual(ids(filterCandidatures(items, { search: 'boréal', etat: 'entrevue' })), [3])
  assert.deepEqual(filterCandidatures(items, { search: 'conseil', etat: 'entrevue' }), [])
})

test('no matches and an initially empty list both return an empty array', () => {
  assert.deepEqual(filterCandidatures(items, { search: 'introuvable', etat: '' }), [])
  assert.deepEqual(filterCandidatures([], { search: 'react', etat: 'envoyee' }), [])
})

test('resetting criteria restores all loaded records', () => {
  let criteria = { search: 'react', etat: 'entrevue' }
  assert.deepEqual(ids(filterCandidatures(items, criteria)), [3])
  criteria = { search: '', etat: '' }
  assert.deepEqual(ids(filterCandidatures(items, criteria)), [3, 2, 1])
})

test('filtering preserves source objects, order and array without sorting or mutation', () => {
  const frozen = Object.freeze(items.map((item) => Object.freeze({ ...item })))
  const result = filterCandidatures(frozen, { search: 'react', etat: '' })
  assert.deepEqual(ids(result), [3, 1])
  assert.equal(result[0], frozen[0])
  assert.equal(result[1], frozen[2])
  assert.notEqual(result, frozen)
  assert.deepEqual(ids(frozen), [3, 2, 1])
})

test('criteria are reapplied to refreshed CRUD data', () => {
  const criteria = { search: 'react', etat: 'envoyee' }
  assert.deepEqual(ids(filterCandidatures(items, criteria)), [1])
  const created = { ...items[0], id: 4, etat: 'envoyee' }
  assert.deepEqual(ids(filterCandidatures([created, ...items], criteria)), [4, 1])
  const updated = items.map((item) => item.id === 1 ? { ...item, etat: 'retiree' } : item)
  assert.deepEqual(filterCandidatures(updated, criteria), [])
  assert.deepEqual(filterCandidatures(items.filter((item) => item.id !== 1), criteria), [])
})
