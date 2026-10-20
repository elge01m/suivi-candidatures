import { useEffect, useState } from 'react'
import { ETATS } from './candidatures'
import { HISTORY_FIELDS, listHistory } from './tracking'
import type { HistoryEntry } from './tracking'

function valueLabel(field:string,value:string|boolean|null|undefined) {
  if(value===undefined) return '—'
  if(value===null) return 'Non renseigné'
  if(typeof value==='boolean') return value?'Oui':'Non'
  return field==='etat'?ETATS.find(state=>state.value===value)?.label??value:value
}
export function HistoryEntries({entries}:{entries:HistoryEntry[]}) {
  if(!entries.length) return <p>Aucun changement enregistré. L’historique commence à son activation ; les changements anciens ne sont pas reconstitués.</p>
  return <ol className="history-list">{entries.map(entry=><li key={entry.id}>
    <p>{new Date(entry.date).toLocaleString('fr-CA')} — {entry.origine==='manuelle'?'Modification manuelle':'Proposition acceptée'}</p>
    {entry.source_message_reference!==null&&<p>Courriel local nº {entry.source_message_reference}{entry.source_message_id===null?' (source supprimée ; référence conservée)':''}.</p>}
    {entry.source_proposition_reference&&<p>Proposition : {entry.source_proposition_reference}</p>}
    <div className="history-table"><table><thead><tr><th>Champ</th><th>Avant</th><th>Après</th></tr></thead>
      <tbody>{Object.keys(entry.apres).map(field=><tr key={field}><th>{HISTORY_FIELDS[field]}</th>
        <td>{valueLabel(field,entry.avant[field])}</td><td>{valueLabel(field,entry.apres[field])}</td></tr>)}</tbody></table></div>
  </li>)}</ol>
}
export default function HistoryPanel({id,version}:{id:number;version:unknown}) {
  const [open,setOpen]=useState(false)
  const [entries,setEntries]=useState<HistoryEntry[]>([])
  const [error,setError]=useState('')
  const [loading,setLoading]=useState(false)
  const [reload,setReload]=useState(0)
  useEffect(()=>{
    if(!open) return
    const controller=new AbortController()
    listHistory(id,controller.signal).then(rows=>{if(!controller.signal.aborted){setEntries(rows);setError('')}})
      .catch(cause=>{if(!controller.signal.aborted)setError(cause instanceof Error?cause.message:'Historique indisponible.')})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false)})
    return ()=>controller.abort()
  },[open,id,version,reload])
  return <div className="history-panel">
    <button type="button" className="secondary" aria-expanded={open} onClick={()=>{setLoading(!open);setError('');setOpen(value=>!value)}}>{open?'Masquer l’historique':'Consulter l’historique'}</button>
    {open&&<section aria-label="Historique de la candidature" aria-busy={loading}>
      {loading?<p role="status">Chargement de l’historique…</p>:error?<p role="alert" className="error">{error}</p>:<HistoryEntries entries={entries}/>}
      <button type="button" className="secondary" disabled={loading} onClick={()=>{setLoading(true);setError('');setReload(value=>value+1)}}>Actualiser l’historique</button>
    </section>}
  </div>
}
