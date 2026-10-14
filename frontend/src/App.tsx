import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

type Result = { status: 'success'|'error'|'refused'; summary: string; sql?: string|null; columns: string[]; rows: Record<string, string|number|null>[]; duration_ms: number; correction_attempts: number; error?: string|null }
type Schema = { tables: Record<string, {columns: Record<string,{type:string;nullable:boolean;primary_key:boolean}>; primary_keys:string[]; foreign_keys:{column:string;referenced_table:string;referenced_column:string}[]}>; relationships: string[]; revenue_definition?: string; sample_date_context?: string }
type Database = { id:string; name:string; is_sample:boolean }
const API = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const examples = ['What was our total revenue last quarter?', 'Which product categories generated the most revenue?', 'Which customers placed the most orders?', 'What were our top-selling products last month?']

export default function App() {
  const [question, setQuestion] = useState('')
  const [result, setResult] = useState<Result|null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [schema, setSchema] = useState<Schema|null>(null)
  const [showSchema, setShowSchema] = useState(false)
  const [sessionId, setSessionId] = useState(localStorage.getItem('querypilot-session') || '')
  const [databases, setDatabases] = useState<Database[]>([])
  const [activeDb, setActiveDb] = useState<Database|null>(null)
  const [uploading, setUploading] = useState(false)
  const [dataError, setDataError] = useState('')
  const headers = (extra:Record<string,string>={}) => ({...(sessionId ? {'X-QueryPilot-Session':sessionId} : {}), ...extra})
  async function refreshDataSources(currentSession=sessionId) {
    const response = await fetch(`${API}/api/databases/active`, {headers: currentSession ? {'X-QueryPilot-Session':currentSession} : {}})
    const receivedSession = response.headers.get('X-QueryPilot-Session') || currentSession
    if (!response.ok || !receivedSession) throw new Error((await response.json()).detail || 'Could not load databases.')
    localStorage.setItem('querypilot-session', receivedSession); setSessionId(receivedSession)
    const active = await response.json(); setActiveDb({id:active.id,name:active.name,is_sample:active.is_sample}); setSchema(active.schema)
    const list = await fetch(`${API}/api/databases`, {headers:{'X-QueryPilot-Session':receivedSession}})
    if (!list.ok) throw new Error('Could not load databases.')
    setDatabases((await list.json()).databases)
  }
  useEffect(() => { refreshDataSources().catch(() => setError('Cannot reach the QueryPilot API. Start the backend and try again.')) }, [])
  async function chooseDatabase(database_id:string) {
    setDataError('')
    try {
      const response = await fetch(`${API}/api/databases/active`, {method:'POST',headers:headers({'Content-Type':'application/json'}),body:JSON.stringify({database_id})})
      const body = await response.json(); if (!response.ok) throw new Error(body.detail || 'Could not switch database.')
      await refreshDataSources()
    } catch(e) { setDataError(e instanceof Error ? e.message : 'Could not switch database.') }
  }
  async function uploadDatabase(file?:File) {
    if (!file) return
    setUploading(true); setDataError('')
    try {
      const response = await fetch(`${API}/api/databases/upload`, {method:'POST',headers:sessionId ? {'X-QueryPilot-Session':sessionId} : {},body:(()=>{const form=new FormData();form.append('file',file);return form})()})
      const body = await response.json(); if (!response.ok) throw new Error(body.detail || 'Upload failed.')
      await refreshDataSources()
    } catch(e) { setDataError(e instanceof Error ? e.message : 'Upload failed.') }
    finally { setUploading(false) }
  }
  async function submit(q = question) {
    if (busy || !q.trim()) return
    setQuestion(q); setBusy(true); setError(''); setResult(null)
    try {
      const response = await fetch(`${API}/api/query`, {method:'POST',headers:headers({'Content-Type':'application/json'}),body:JSON.stringify({question:q})})
      const body = await response.json()
      if (!response.ok) throw new Error(body.error || body.detail || 'The request failed. Please try again.')
      setResult(body as Result)
    } catch (e) { setError(e instanceof Error ? e.message : 'The request failed. Please try again.') }
    finally { setBusy(false) }
  }
  const chartable = result?.status === 'success' && result.rows.length > 0 && result.columns.length >= 2 && result.rows.length <= 30 && result.rows.every(row => typeof row[result.columns[1]] === 'number')
  return <main className="shell">
    <header><div className="brand"><span className="mark">Q</span><span>QueryPilot</span></div><span className="badge"><i/> {activeDb?.name || 'Loading data…'}</span></header>
    <section className="data-source"><div><strong>Data source</strong><span>{activeDb?.is_sample ? 'Bundled sample database' : 'Uploaded SQLite database'}</span></div><select aria-label="Active database" value={activeDb?.id || 'sample'} onChange={e=>void chooseDatabase(e.target.value)} disabled={uploading}>{databases.map(db=><option key={db.id} value={db.id}>{db.name}</option>)}</select><label className="upload-button">{uploading?'Uploading…':'Upload SQLite'}<input type="file" accept=".db,.sqlite,.sqlite3" disabled={uploading} onChange={e=>{void uploadDatabase(e.target.files?.[0]);e.currentTarget.value=''}} /></label></section>
    {dataError && <div className="alert">{dataError}</div>}
    <section className="hero"><div className="eyebrow">ASK YOUR DATA</div><h1>Answers, from a question.</h1><p>Explore retail performance in plain English. QueryPilot turns your question into a guarded, inspectable SQL query.</p></section>
    <section className="composer"><label htmlFor="question">What would you like to know?</label><textarea id="question" value={question} maxLength={1000} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();void submit()}}} placeholder="e.g. Which product categories generated the most revenue?"/><div className="compose-bottom"><span>{question.length}/1000</span><button onClick={()=>void submit()} disabled={busy||question.trim().length<3}>{busy?<><span className="spinner"/> Working…</>:'Run query　↗'}</button></div></section>
    <div className="examples"><span>TRY ASKING</span>{examples.map(q=><button key={q} onClick={()=>void submit(q)} disabled={busy}>{q}</button>)}</div>
    {error && <div className="alert">{error}</div>}
    {result && <section className={`result ${result.status}`}><div className="result-head"><div><span className={`status-dot ${result.status}`}/><strong>{result.status==='success'?'Query complete':result.status==='refused'?'Request declined':'Could not run query'}</strong></div><span>{result.duration_ms.toFixed(0)} ms · {result.correction_attempts} correction{result.correction_attempts===1?'':'s'}</span></div><p className="summary">{result.summary}</p>
      {result.status==='success' && <><details className="sql"><summary>Inspect SQL <span>Read-only query</span></summary><pre>{result.sql}</pre></details>
        {chartable && <div className="chart"><h3>{result.columns[1]} by {result.columns[0]}</h3><ResponsiveContainer width="100%" height={260}><BarChart data={result.rows}><CartesianGrid strokeDasharray="3 3" stroke="#e8ebf0"/><XAxis dataKey={result.columns[0]} tick={{fontSize:11}}/><YAxis tick={{fontSize:11}}/><Tooltip/><Bar dataKey={result.columns[1]} fill="#4464e8" radius={[4,4,0,0]}/></BarChart></ResponsiveContainer></div>}
        <div className="table-wrap"><table><thead><tr>{result.columns.map(c=><th key={c}>{c.replaceAll('_',' ')}</th>)}</tr></thead><tbody>{result.rows.map((row,i)=><tr key={i}>{result.columns.map(c=><td key={c}>{row[c]===null?'—':String(row[c])}</td>)}</tr>)}</tbody></table>{!result.rows.length&&<div className="empty">No matching rows.</div>}</div></>}
    </section>}
    <section className="schema"><button className="schema-toggle" onClick={()=>setShowSchema(!showSchema)}>{showSchema?'−':'+'} Explore available data</button>{showSchema&&schema&&<div className="schema-content">{schema.revenue_definition&&<p>{schema.revenue_definition}</p>}<div className="schema-grid">{Object.entries(schema.tables).map(([table,meta])=><div key={table}><b>{table}</b><span>{Object.entries(meta.columns).map(([name,col])=>`${name} · ${col.type||'no type'}${col.primary_key?' · PK':''}${!col.nullable?' · required':''}`).join('  /  ')}{meta.foreign_keys.length>0&&` · References ${meta.foreign_keys.map(f=>`${f.column} → ${f.referenced_table}.${f.referenced_column}`).join(', ')}`}</span></div>)}</div></div>}</section>
    <footer>QueryPilot · Natural language analytics with SQL you can inspect.</footer>
  </main>
}
