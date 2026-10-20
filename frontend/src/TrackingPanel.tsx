import { useEffect, useRef, useState } from 'react'
import { ERROR_LABELS, STATUS_LABELS, listTreatments, retryTreatment } from './tracking'
import type { Treatment } from './tracking'

export function TreatmentRows({rows,busy,onRetry}:{rows:Treatment[];busy:boolean;onRetry:(row:Treatment)=>void}) {
  if(!rows.length)return <p>Aucun traitement enregistré.</p>
  return <ul className="application-list">{rows.map(row=><li className="application-card" key={`${row.kind}-${row.id}`}>
    <h3>{row.kind==='import'?`Import du ${row.date_debut} au ${row.date_fin}`:`Analyse du message local nº ${row.message_reference}`}</h3>
    <p>{STATUS_LABELS[row.status]}</p>
    {row.kind==='import'?<p>{row.processed} examinés · {row.imported} importés · {row.duplicates} doublons évités · {row.skipped} ignorés</p>:<p>Tentatives : {row.attempts}</p>}
    {row.error_code&&<p className="error">{Object.hasOwn(ERROR_LABELS,row.error_code)?ERROR_LABELS[row.error_code]:'Le traitement a rencontré une erreur. Vérifiez la connexion ou la configuration.'}</p>}
    {row.retry_after&&<p>Reprise possible à partir du {new Date(row.retry_after).toLocaleString('fr-CA')}.</p>}
    {['failed','paused'].includes(row.status)&&!row.can_retry&&<p>Relance indisponible actuellement : délai, limite de tentatives, traitement actif ou vérification de connexion nécessaire.</p>}
    {row.can_retry&&<button type="button" className="secondary" disabled={busy} onClick={()=>onRetry(row)}>
      {row.kind==='analysis'?'Réessayer avec un nouveau consentement…':'Remettre l’import en attente…'}</button>}
  </li>)}</ul>
}
export default function TrackingPanel({disabled,onPendingChange,onUpdated}:{disabled:boolean;onPendingChange:(busy:boolean)=>void;onUpdated:()=>void}) {
  const [rows,setRows]=useState<Treatment[]>([])
  const [loading,setLoading]=useState(true)
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const [reload,setReload]=useState(0)
  const submitting=useRef(false)
  useEffect(()=>{
    const controller=new AbortController()
    listTreatments(controller.signal).then(data=>{if(!controller.signal.aborted){setRows(data);setError('')}})
      .catch(cause=>{if(!controller.signal.aborted)setError(cause instanceof Error?cause.message:'Suivi indisponible.')})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false)})
    return ()=>controller.abort()
  },[reload])
  async function retry(row:Treatment) {
    if(disabled||busy||loading||submitting.current)return
    submitting.current=true;setBusy(true);onPendingChange(true);setError('');setNotice('')
    try {
      const done=await retryTreatment(row,()=>window.confirm(row.kind==='analysis'
        ?'Réessayer cette analyse ? Je consens explicitement à envoyer de nouveau le sujet et le texte de ce message à Gemini. Aucune candidature ne sera modifiée sans validation humaine.'
        :'Remettre cet import en attente du serveur, en conservant les compteurs et la prévention des doublons ?'))
      if(done){setNotice(row.kind==='import'?'Import remis en attente. Le worker serveur doit traiter les lots.':'Nouvelle tentative terminée. Consultez son résultat ; aucune proposition n’a été appliquée automatiquement.');setLoading(true);setReload(value=>value+1);onUpdated()}
    }catch(cause){setError(cause instanceof Error?cause.message:'Relance indisponible.');setNotice('Actualisez le suivi avant de réessayer : une réponse réseau perdue ne signifie pas que le traitement a échoué.')}
    finally{submitting.current=false;setBusy(false);onPendingChange(false)}
  }
  return <section className="sync-panel" aria-label="Suivi des traitements" aria-busy={loading||busy}>
    <div className="list-toolbar"><h2>Traitements et erreurs</h2><button type="button" className="secondary" disabled={disabled||loading||busy} onClick={()=>{setLoading(true);setReload(value=>value+1)}}>Actualiser les traitements</button></div>
    <p>Les 100 derniers imports et les 100 dernières analyses. Les relances restent soumises aux délais, quotas et vérifications de connexion. Aucune analyse ne repart sans votre consentement explicite.</p>
    {error&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {loading?<p role="status">Chargement des traitements…</p>:<TreatmentRows rows={rows} busy={disabled||busy||Boolean(error)} onRetry={row=>void retry(row)}/>}
  </section>
}
