import { ETATS } from './candidatures'
import type { CandidatureCriteria } from './candidatureFilters'

type Props = {
  criteria: CandidatureCriteria
  disabled: boolean
  onChange: (criteria: CandidatureCriteria) => void
}

export default function CandidatureFilters({ criteria, disabled, onChange }: Props) {
  return (
    <fieldset className="list-filters" disabled={disabled}>
      <legend className="sr-only">Rechercher et filtrer les candidatures</legend>
      <div className="field">
        <label htmlFor="candidature-search">Rechercher</label>
        <input id="candidature-search" type="search" value={criteria.search}
          placeholder="Employeur ou intitulé de poste"
          onChange={(event) => onChange({ ...criteria, search: event.target.value })} />
      </div>
      <div className="field">
        <label htmlFor="candidature-state-filter">Filtrer par état</label>
        <select id="candidature-state-filter" value={criteria.etat}
          onChange={(event) => onChange({ ...criteria, etat: event.target.value as CandidatureCriteria['etat'] })}>
          <option value="">Tous les états</option>
          {ETATS.map((state) => <option key={state.value} value={state.value}>{state.label}</option>)}
        </select>
      </div>
      <button className="secondary" type="button" disabled={!criteria.search && !criteria.etat}
        onClick={() => onChange({ search: '', etat: '' })}>Réinitialiser les critères</button>
    </fieldset>
  )
}
