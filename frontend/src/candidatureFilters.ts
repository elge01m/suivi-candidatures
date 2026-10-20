import type { Candidature, Etat } from './candidatures.ts'

export type CandidatureCriteria = { search: string; etat: Etat | '' }

export function filterCandidatures(
  items: readonly Candidature[], criteria: CandidatureCriteria,
): Candidature[] {
  const search = criteria.search.trim().normalize('NFC').toLocaleLowerCase('fr')
  return items.filter((item) => {
    const matchesSearch = !search || [item.employeur, item.poste].some((value) =>
      value.normalize('NFC').toLocaleLowerCase('fr').includes(search),
    )
    return matchesSearch && (!criteria.etat || item.etat === criteria.etat)
  })
}
