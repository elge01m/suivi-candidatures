export const id = '12345678-1234-4234-8234-123456789abc'
export const secondId = '12345678-1234-4234-8234-123456789abd'
export const proposal = {
  id, status: 'en_attente', revision: 1, resultat_id: null,
  proposition: {
    pertinent: true, action: 'changement_etat', employeur: 'ExempleTech', poste: 'Développeur logiciel',
    confirmation_reception: null, date_candidature: null, etat: 'entrevue', justification: 'Invitation fictive.',
    informations_manquantes: ['date_candidature'], candidatures_possibles: [7], niveau_association: 'probable', ambiguite: false,
    correspondances_tronquees: false, cible_id: null, cible_version: null, contenu_incomplet: false,
  },
}
export const candidate = {
  id: 7, employeur: 'ExempleTech', poste: 'Développeur logiciel', date_candidature: '2026-10-12',
  etat: 'envoyee', confirmation_reception: null, lien: '', notes: '',
}
export const creation = {
  ...proposal, proposition: { ...proposal.proposition, action: 'creation', date_candidature: '2026-10-12', informations_manquantes: [] },
}
export const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
