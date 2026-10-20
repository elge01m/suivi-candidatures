import { ApiError, initializeCsrf, request } from './auth.ts'

export const ERROR_LABELS: Record<string, string> = {
  gmail_unavailable:'Gmail est temporairement indisponible.', rate_limited:'Limite API atteinte ; respectez le délai de reprise.',
  reauthorization_required:'Une reconnexion Gmail est nécessaire.', mailbox_changed:'La boîte Gmail a changé ; vérifiez la connexion.',
  credentials_changed:'La connexion a changé ; vérifiez-la avant un nouvel import.', gmail_access_denied:'Google refuse cet accès.',
  invalid_page:'La pagination a expiré ; créez un nouvel import de la même période.', internal_error:'Le traitement a été interrompu.',
  time_budget:'Le lot doit être poursuivi.', lease_lost:'Le traitement a été interrompu.',
  provider_unavailable:'Gemini est temporairement indisponible.', provider_denied:'L’accès Gemini est refusé.',
  invalid_response:'Gemini a renvoyé une réponse inexploitable.', blocked_response:'Gemini n’a pas produit de réponse exploitable.',
  quota_exceeded:'Le budget local est atteint.', attempts_exhausted:'Le nombre maximal de tentatives est atteint.',
  consent_revoked:'Le consentement a été révoqué.', configuration_missing:'La configuration serveur est indisponible.',
}
export const STATUS_LABELS: Record<string, string> = {
  ready:'Prêt', queued:'En attente', running:'En cours', paused:'En pause', completed:'Terminé', failed:'Échec', cancelled:'Annulé',
}
export type Treatment = { id:string; kind:'import'|'analysis'; status:string; error_code:string; retry_after:string|null; can_retry:boolean
  date_debut?:string; date_fin?:string; processed?:number; imported?:number; duplicates?:number; skipped?:number; attempts?:number; message_reference?:number }
function object(value:unknown):value is Record<string,unknown> { return typeof value==='object' && value!==null && !Array.isArray(value) }
function invalid():never { throw new Error('Réponse de suivi inattendue. Actualisez les données.') }
async function read(path:string, options:RequestInit={}):Promise<unknown> {
  try { const response=await request(path,options); return await response.json() }
  catch(error) {
    if(error instanceof ApiError) {
      if(error.status===401||error.status===403) throw new Error('Session expirée ou protection CSRF refusée.')
      if(error.code==='retry_unavailable') throw new Error('Relance indisponible : respectez le délai et les limites de tentatives.')
      if(error.code==='quota_exceeded') throw new Error(ERROR_LABELS.quota_exceeded)
      throw new Error('Opération indisponible. Actualisez le suivi avant de réessayer.')
    }
    if(error instanceof SyntaxError) return invalid()
    throw error
  }
}
function parseTreatment(value:unknown,kind:Treatment['kind']):Treatment {
  if(!object(value)||typeof value.id!=='string'||!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value.id)||
    typeof value.status!=='string'||!Object.hasOwn(STATUS_LABELS,value.status)||typeof value.error_code!=='string'||
    typeof value.can_retry!=='boolean'||!(value.retry_after===null||typeof value.retry_after==='string'&&!Number.isNaN(Date.parse(value.retry_after)))) return invalid()
  const row:Treatment={id:value.id,kind,status:value.status,error_code:value.error_code,retry_after:value.retry_after,can_retry:value.can_retry}
  const counts=kind==='import'?['processed','imported','duplicates','skipped']:['attempts','message_reference']
  for(const key of counts) {
    if(typeof value[key]!=='number'||!Number.isSafeInteger(value[key])||value[key]<0) return invalid()
    Object.assign(row,{[key]:value[key]})
  }
  if(kind==='import') {
    if(typeof value.date_debut!=='string'||typeof value.date_fin!=='string') return invalid()
    row.date_debut=value.date_debut; row.date_fin=value.date_fin
  }
  return row
}
export async function listTreatments(signal?:AbortSignal):Promise<Treatment[]> {
  const bodies=await Promise.all([read('/api/gmail/imports/',{signal}),read('/api/analyses/',{signal})])
  return bodies.flatMap((body,index)=>{
    if(!object(body)||!Array.isArray(body.results)) return invalid()
    return body.results.map(value=>parseTreatment(value,index===0?'import':'analysis'))
  })
}
export async function retryTreatment(row:Treatment,confirm:()=>boolean):Promise<boolean> {
  if(!['import','analysis'].includes(row.kind)) return invalid()
  if(!row.can_retry) throw new Error('Ce traitement ne peut pas être relancé actuellement.')
  if(!confirm()) return false
  if(!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(row.id)) return invalid()
  const token=await initializeCsrf()
  await read(row.kind==='import'?`/api/sync/imports/${row.id}/retry/`:`/api/analyses/${row.id}/retry/`,{
    method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':token},
    body:JSON.stringify(row.kind==='import'?{confirmer:true}:{consentement:true}),
  })
  return true
}

export const HISTORY_FIELDS:Record<string,string>={employeur:'Employeur',poste:'Poste',date_candidature:'Date de candidature',
  etat:'État',confirmation_reception:'Confirmation de réception',lien:'Lien',notes:'Notes'}
export type HistoryEntry={id:number;date:string;origine:'manuelle'|'proposition';avant:Record<string,string|boolean|null>;apres:Record<string,string|boolean|null>;
  source_message_reference:number|null;source_proposition_reference:string|null;source_message_id:number|null}
export async function listHistory(id:number,signal?:AbortSignal):Promise<HistoryEntry[]> {
  if(!Number.isSafeInteger(id)||id<1) return invalid()
  const body=await read(`/api/candidatures/${id}/historique/`,{signal})
  if(!object(body)||!Array.isArray(body.results)) return invalid()
  return body.results.map(value=>{
    if(!object(value)||typeof value.id!=='number'||!Number.isSafeInteger(value.id)||value.id<1||typeof value.date!=='string'||Number.isNaN(Date.parse(value.date))||
      !['manuelle','proposition'].includes(String(value.origine))||!object(value.avant)||!object(value.apres)) return invalid()
    for(const field of ['source_message_reference','source_message_id']) {
      const item=value[field]
      if(!(item===null||typeof item==='number'&&Number.isSafeInteger(item)&&item>0)) return invalid()
    }
    if(!(value.source_proposition_reference===null||typeof value.source_proposition_reference==='string'&&/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value.source_proposition_reference))) return invalid()
    for(const data of [value.avant,value.apres]) for(const [field,item] of Object.entries(data)) {
      if(!Object.hasOwn(HISTORY_FIELDS,field)||!(item===null||typeof item==='string'||typeof item==='boolean')) return invalid()
    }
    return {id:value.id,date:value.date,origine:value.origine,avant:value.avant,apres:value.apres,
      source_message_reference:value.source_message_reference,source_proposition_reference:value.source_proposition_reference,source_message_id:value.source_message_id} as HistoryEntry
  })
}
