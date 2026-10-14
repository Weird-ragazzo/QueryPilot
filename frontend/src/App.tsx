import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from 'recharts'

type Visualization = {type:'bar'|'line'|'pie'|'donut'|'scatter'|null;label_column:string|null;x_column:string|null;series_columns:string[];reason?:string|null;alternative?:string|null;omitted_rows?:number}
type Result = { status: 'success'|'error'|'refused'; summary: string; sql?: string|null; columns: string[]; rows: Record<string, string|number|null>[]; duration_ms: number; latency_ms?:number|null; correction_attempts: number; error?: string|null; response_type?:'query'|'visualization'|'summary'|'comparison'|'examine'; visualization?:Visualization|null; statistics?:Record<string,unknown>|null; conversation_id?:string; result_id?:string; database_ids?:string[] }
type SchemaObject = {columns: Record<string,{type:string;nullable:boolean;primary_key:boolean}>; primary_keys:string[]; foreign_keys:{column:string;referenced_table:string;referenced_column:string}[]; depends_on?:string[]}
type Schema = { tables: Record<string, SchemaObject>; views?: Record<string, SchemaObject>; relationships: string[]; revenue_definition?: string; sample_date_context?: string; source_names?:Record<string,string>; source_type?:string }
type Database = { id:string; name:string; is_sample:boolean; source_type?:string; original_filename?:string|null }
type ChatMessage = {role:'user'|'assistant';content:string;database_ids?:string[];database_names?:string[];result_id?:string;historical_source_deleted?:boolean;columns?:string[];response_type?:string;result?:{columns?:string[];rows?:Record<string,string|number|null>[];sql?:string|null;response_type?:string;result_not_retained?:string}}
const API = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const examples = ['What was our total revenue last quarter?', 'Which product categories generated the most revenue?', 'Which customers placed the most orders?', 'What were our top-selling products last month?']

export default function App() {
  const [question, setQuestion] = useState('')
  const [result, setResult] = useState<Result|null>(null)
  const [chartOverride, setChartOverride] = useState<Visualization|null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [showSchema, setShowSchema] = useState(false)
  const [sessionId, setSessionId] = useState(localStorage.getItem('querypilot-session') || '')
  const [databases, setDatabases] = useState<Database[]>([])
  const [uploading, setUploading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [dataError, setDataError] = useState('')
  const [dataNotice, setDataNotice] = useState('')
  const [maxUploadBytes, setMaxUploadBytes] = useState(26214400)
  const [maxImportRows, setMaxImportRows] = useState(100000)
  const [maxImportSheets, setMaxImportSheets] = useState(20)
  const [maxImportCells, setMaxImportCells] = useState(2000000)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [conversationId, setConversationId] = useState(localStorage.getItem('querypilot-conversation') || '')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [selectedSchemas, setSelectedSchemas] = useState<{id:string;name:string;schema:Schema}[]>([])
  const headers = (extra:Record<string,string>={}) => ({...(sessionId ? {'X-QueryPilot-Session':sessionId} : {}), ...extra})
  async function refreshDataSources(currentSession=sessionId) {
    const response = await fetch(`${API}/api/databases`, {headers: currentSession ? {'X-QueryPilot-Session':currentSession} : {}})
    const receivedSession = response.headers.get('X-QueryPilot-Session') || currentSession
    const listBody = await response.json()
    if (!response.ok || !receivedSession) throw new Error(listBody.detail || 'Could not load databases.')
    localStorage.setItem('querypilot-session', receivedSession); setSessionId(receivedSession)
    setDatabases(listBody.databases)
    setMaxUploadBytes(listBody.max_upload_bytes || 26214400)
    setMaxImportRows(listBody.max_import_rows || 100000)
    setMaxImportSheets(listBody.max_import_sheets || 20)
    setMaxImportCells(listBody.max_import_cells || 2000000)
    setSelectedIds(current => current.length && current.every(id => listBody.databases.some((db:Database)=>db.id===id)) ? current : [listBody.active_id])
    let currentConversation=localStorage.getItem('querypilot-conversation') || ''
    if (currentConversation) {
      const saved=await fetch(`${API}/api/conversations/${currentConversation}`,{headers:{'X-QueryPilot-Session':receivedSession}})
      if(saved.ok){const body=await saved.json();setMessages((body.messages || []).map((m:ChatMessage)=>({...m,columns:m.result?.columns||[],response_type:m.result?.response_type})))}else currentConversation=''
    }
    if(!currentConversation){const created=await fetch(`${API}/api/conversations`,{method:'POST',headers:{'X-QueryPilot-Session':receivedSession}});const body=await created.json();if(created.ok){currentConversation=body.conversation_id;setMessages([])}}
    if(currentConversation){localStorage.setItem('querypilot-conversation',currentConversation);setConversationId(currentConversation)}
  }
  useEffect(() => { refreshDataSources().catch(e => setError(e instanceof Error ? e.message : 'Cannot reach the QueryPilot API. Start the backend and try again.')) }, [])
  useEffect(() => {
    if(!sessionId||!selectedIds.length)return
    let cancelled=false
    Promise.all(selectedIds.map(async id=>{const response=await fetch(`${API}/api/databases/${id}/schema`,{headers:headers()});if(!response.ok)return null;return await response.json()})).then(items=>{if(!cancelled)setSelectedSchemas(items.filter(Boolean))}).catch(()=>{if(!cancelled)setSelectedSchemas([])})
    return ()=>{cancelled=true}
  },[sessionId,selectedIds.join(',')])
  async function uploadDatabase(file?:File) {
    if (!file) return
    setUploading(true); setDataError(''); setDataNotice('')
    try {
      const response = await fetch(`${API}/api/databases/upload`, {method:'POST',headers:sessionId ? {'X-QueryPilot-Session':sessionId} : {},body:(()=>{const form=new FormData();form.append('file',file);return form})()})
      const body = await response.json(); if (!response.ok) throw new Error(body.detail || 'Upload failed.')
      const receivedSession=response.headers.get('X-QueryPilot-Session') || sessionId
      if(receivedSession){localStorage.setItem('querypilot-session',receivedSession);setSessionId(receivedSession)}
      await refreshDataSources(receivedSession)
      if(body.id)setSelectedIds([body.id])
      setDataNotice(`${body.original_filename || file.name} uploaded and ready.`)
    } catch(e) { setDataError(e instanceof Error ? e.message : 'Upload failed.') }
    finally { setUploading(false) }
  }
  async function submit(q = question) {
    if (busy || !q.trim()) return
    setQuestion(q); setBusy(true); setError(''); setResult(null); setChartOverride(null)
    try {
      const response = await fetch(`${API}/api/query`, {method:'POST',headers:headers({'Content-Type':'application/json'}),body:JSON.stringify({question:q,database_ids:selectedIds,conversation_id:conversationId || undefined})})
      const body = await response.json()
      if (!response.ok) throw new Error(body.error || body.detail || 'The request failed. Please try again.')
      setResult(body as Result)
      if(body.conversation_id){setConversationId(body.conversation_id);localStorage.setItem('querypilot-conversation',body.conversation_id)}
      setMessages(current=>[...current,{role:'user' as const,content:q,database_ids:body.database_ids||selectedIds,database_names:body.database_names||[]},{role:'assistant' as const,content:body.summary,database_ids:body.database_ids||selectedIds,database_names:body.database_names||[],result_id:body.result_id,columns:body.columns||[],response_type:body.response_type,result:{columns:body.columns||[],rows:body.rows||[],sql:body.sql}}].slice(-100))
    } catch (e) { setError(e instanceof Error ? e.message : 'The request failed. Please try again.') }
    finally { setBusy(false) }
  }
  async function startConversation() {
    try{const response=await fetch(`${API}/api/conversations`,{method:'POST',headers:headers()});const body=await response.json()
      if(!response.ok)throw new Error(body.detail||'Could not start a new conversation.')
      setConversationId(body.conversation_id);localStorage.setItem('querypilot-conversation',body.conversation_id);setMessages([]);setResult(null);setError('')
    }catch(e){setError(e instanceof Error?e.message:'Could not start a new conversation.')}
  }
  async function deleteDatabase(id:string) {
    if(id==='sample'||!window.confirm(`Delete ${databases.find(db=>db.id===id)?.name || 'this uploaded source'} and its stored file?`))return
    setDataError('')
    try{const response=await fetch(`${API}/api/databases/${id}`,{method:'DELETE',headers:headers()});const body=await response.json()
      if(!response.ok)throw new Error(body.detail||'Could not delete this source.')
      setSelectedIds(current=>current.filter(sourceId=>sourceId!==id));setResult(null);await refreshDataSources()
    }catch(e){setDataError(e instanceof Error?e.message:'Could not delete this source.')}
  }
  async function exportResult(kind:'csv'|'pdf',resultId:string){
    setExporting(true);setError('')
    try{const response=await fetch(`${API}/api/conversations/${conversationId}/results/${resultId}/${kind}`,{headers:headers()})
      if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||`Could not export ${kind.toUpperCase()}.`)}
      const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`querypilot-${resultId.slice(0,12)}.${kind}`;document.body.append(link);link.click();link.remove();URL.revokeObjectURL(url)
    }catch(e){setError(e instanceof Error?e.message:`Could not export ${kind.toUpperCase()}.`)}finally{setExporting(false)}
  }
  const chart = chartOverride || result?.visualization
  const chartColumnsPresent = !!chart && !!chart.x_column && result?.columns.includes(chart.x_column) && (!chart.label_column || result?.columns.includes(chart.label_column)) && chart.series_columns.length > 0 && chart.series_columns.every(column => result?.columns.includes(column))
  const chartRows = chartColumnsPresent && result ? result.rows.filter(row => row[chart!.x_column!] != null && chart!.series_columns.every(column => typeof row[column] === 'number' && Number.isFinite(row[column] as number))) : []
  const chartValid = !!chart && ['bar','line','pie','donut','scatter'].includes(chart.type || '') && chartColumnsPresent && chartRows.length > 0 && ((chart.type !== 'pie' && chart.type !== 'donut') || (new Set(chartRows.map(row => row[chart.label_column!])).size <= 20 && chartRows.every(row => (row[chart.series_columns[0]] as number) >= 0) && chartRows.reduce((sum,row) => sum + (row[chart.series_columns[0]] as number),0) > 0))
  const suggestionSchema=selectedSchemas[0]?.schema
  const selectedDatabase=databases.find(db=>db.id===selectedIds[0])
  const suggestionTable=Object.keys(suggestionSchema?.tables||{})[0]
  const suggestionColumns=Object.entries(suggestionSchema?.tables[suggestionTable]?.columns||{})
  const categoryColumn=suggestionColumns.find(([,meta])=>!/(INT|REAL|NUM|DEC|FLOAT|DOUBLE)/i.test(meta.type))?.[0]
  const numericColumn=suggestionColumns.find(([,meta])=>/(INT|REAL|NUM|DEC|FLOAT|DOUBLE)/i.test(meta.type))?.[0]
  const suggestions=selectedIds.length>1?['Summarize selected data sources independently','Examine data quality in the selected sources']:selectedDatabase?.is_sample?examples:suggestionTable?[`How many rows are in ${suggestionTable}?`,...(categoryColumn?[`Compare records by ${categoryColumn}`]:[]),...(numericColumn?[`Summarize ${numericColumn} in ${suggestionTable}`]:[]),`Summarize ${selectedDatabase?.name||'this dataset'}`]:examples
  const renderHistoricalResult=(message:ChatMessage)=>message.role==='assistant'&&message.result_id&&message.result?.columns?.length?<details className="message-result"><summary>Inspect previous SQL and result table</summary>{message.result.sql&&<pre>{message.result.sql}</pre>}<div className="table-wrap"><table><thead><tr>{message.result.columns.map(column=><th key={column}>{column}</th>)}</tr></thead><tbody>{(message.result.rows||[]).map((row,index)=><tr key={index}>{message.result?.columns?.map(column=><td key={column}>{row[column]===null?'—':String(row[column])}</td>)}</tr>)}</tbody></table></div></details>:null
  const renderChart = (valid: boolean) => {
    if (!valid || !chart || !result) return null
    const x = chart.x_column!, measure = chart.series_columns[0]
    if (chart.type === 'pie' || chart.type === 'donut') return <ResponsiveContainer width="100%" height={260}><PieChart><Tooltip/><Pie data={chartRows} dataKey={measure} nameKey={chart.label_column || x} innerRadius={chart.type === 'donut' ? 58 : 0} outerRadius={95} label>{chartRows.map((_,i)=><Cell key={i} fill={['#4665e8','#36a783','#e6a94d','#bc6ae0','#e16c64'][i%5]}/>)}</Pie></PieChart></ResponsiveContainer>
    if (chart.type === 'line') return <ResponsiveContainer width="100%" height={260}><LineChart data={chartRows}><CartesianGrid strokeDasharray="3 3" stroke="#e8ebf0"/><XAxis dataKey={x}/><YAxis/><Tooltip/>{chart.series_columns.map((column,i)=><Line key={column} type="monotone" dataKey={column} stroke={['#4665e8','#36a783','#e6a94d'][i%3]} dot={false}/>)}</LineChart></ResponsiveContainer>
    if (chart.type === 'scatter') return <ResponsiveContainer width="100%" height={260}><ScatterChart><CartesianGrid strokeDasharray="3 3" stroke="#e8ebf0"/><XAxis type="number" dataKey={x} name={x}/><YAxis type="number" dataKey={measure} name={measure}/><Tooltip cursor={{strokeDasharray:'3 3'}}/><Scatter data={chartRows} fill="#4665e8"/></ScatterChart></ResponsiveContainer>
    return <ResponsiveContainer width="100%" height={260}><BarChart data={chartRows}><CartesianGrid strokeDasharray="3 3" stroke="#e8ebf0"/><XAxis dataKey={x}/><YAxis/><Tooltip/>{chart.series_columns.map((column,i)=><Bar key={column} dataKey={column} fill={['#4665e8','#36a783','#e6a94d'][i%3]} radius={[4,4,0,0]}/>)}</BarChart></ResponsiveContainer>
  }
  function changeChart(kind:'bar'|'line'|'pie'|'donut'|'scatter') {
    if(!result)return
    const numeric=result.columns.filter(column=>result.rows.some(row=>typeof row[column]==='number'&&Number.isFinite(row[column] as number)))
    const categories=result.columns.filter(column=>!numeric.includes(column))
    if((kind==='scatter'&&numeric.length<2)||((kind==='bar'||kind==='line'||kind==='pie'||kind==='donut')&&(!categories.length||!numeric.length))){setChartOverride({type:null,label_column:null,x_column:null,series_columns:[],reason:`A ${kind} chart is not suitable for the returned columns.`,alternative:kind==='scatter'?'Return two numeric measures.':'Return a category and a numeric measure.'});return}
    const label=categories[0], x=kind==='scatter'?numeric[0]:label, measures=kind==='scatter'?[numeric[1]]:[numeric[0]]
    const distinct=new Set(result.rows.map(row=>row[label]).filter(value=>value!=null))
    if((kind==='pie'||kind==='donut')&&(distinct.size>20||result.rows.some(row=>typeof row[numeric[0]]==='number'&&(row[numeric[0]] as number)<0))){setChartOverride({type:null,label_column:null,x_column:null,series_columns:[],reason:`A ${kind} chart needs at most 20 categories and non-negative values.`,alternative:'Choose a bar chart instead.'});return}
    setChartOverride({type:kind,label_column:kind==='scatter'?null:label,x_column:x,series_columns:measures})
  }
  return <main className="shell">
    <header><div className="brand"><span className="mark">Q</span><span>QueryPilot</span></div><span className="badge"><i/> {selectedIds.length>1?`${selectedIds.length} sources selected`:selectedDatabase?.name||'Loading data…'}</span></header>
    <section className="data-source"><div><strong>Data sources</strong><span>{selectedIds.length} selected for the next question</span></div><fieldset className="source-checkboxes" disabled={busy||uploading}><legend>Select one or more sources</legend><div>{databases.map(db=><div className="source-option" key={db.id}><label><input type="checkbox" checked={selectedIds.includes(db.id)} disabled={selectedIds.length===1&&selectedIds.includes(db.id)} onChange={e=>setSelectedIds(current=>e.target.checked?[...current,db.id]:current.filter(id=>id!==db.id))}/>{db.name}</label>{!db.is_sample&&<button className="source-delete" type="button" disabled={busy||uploading} aria-label={`Delete ${db.name}`} onClick={()=>void deleteDatabase(db.id)}>Delete</button>}</div>)}</div><small>Selected sources can be summarized or examined independently. Cross-source SQL joins aren’t supported.</small></fieldset><button className="upload-button" disabled={busy||uploading} onClick={()=>void startConversation()}>New conversation</button><label className="upload-button file-upload">{uploading?'Uploading…':'Upload data'}<input type="file" accept=".db,.sqlite,.sqlite3,.csv,.tsv,.xlsx" disabled={uploading||busy} onChange={e=>{void uploadDatabase(e.target.files?.[0]);e.currentTarget.value=''}} /></label><small>File limit: {Math.max(1,Math.round(maxUploadBytes/1048576))} MB · CSV/TSV/XLSX: {maxImportCells.toLocaleString()} cells, {maxImportRows.toLocaleString()} rows per sheet{maxImportSheets>1?`, ${maxImportSheets} sheets`:''}</small></section>
    {dataError && <div className="alert">{dataError}</div>}{dataNotice && <div className="upload-success">{dataNotice}</div>}
    <section className="hero"><div className="eyebrow">ASK YOUR DATA</div><h1>Answers, from a question.</h1><p>Ask questions, inspect results, and profile selected datasets with guarded, read-only analytics.</p></section>
    <section className="composer"><label htmlFor="question">What would you like to know?</label><textarea id="question" value={question} maxLength={1000} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();void submit()}}} placeholder="e.g. Which product categories generated the most revenue?"/><div className="compose-bottom"><span>{question.length}/1000</span><button onClick={()=>void submit()} disabled={busy||question.trim().length<3}>{busy?<><span className="spinner"/> Working…</>:'Run query　↗'}</button></div></section>
    <div className="examples"><span>TRY ASKING</span>{suggestions.map(q=><button key={q} onClick={()=>void submit(q)} disabled={busy}>{q}</button>)}<button onClick={()=>{setQuestion('Examine data quality');void submit('Examine data quality')}} disabled={busy}>Examine data</button></div>
    {messages.length>0&&<section className="conversation"><strong>Conversation · temporary for 12 hours</strong>{messages.map((message,index)=><div key={`${index}-${message.role}`} className={`message ${message.role}`}><b>{message.role==='user'?'You':'QueryPilot'}</b><span>{message.content}</span>{message.database_names&&message.database_names.length>0&&<small>Sources: {message.database_names.join(', ')}</small>}{message.role==='assistant'&&message.result_id&&message.columns&&message.columns.length>0&&<button disabled={exporting} onClick={()=>void exportResult('csv',message.result_id!)}>{exporting?'Preparing export…':'Export CSV'}</button>}{message.role==='assistant'&&message.result_id&&<button disabled={exporting} onClick={()=>void exportResult('pdf',message.result_id!)}>{exporting?'Preparing export…':'Export PDF'}</button>}{renderHistoricalResult(message)}{message.historical_source_deleted&&<small>Source deleted · historical result</small>}</div>)}</section>}
    {error && <div className="alert">{error}</div>}
    {result && <section className={`result ${result.status}`}><div className="result-head"><div><span className={`status-dot ${result.status}`}/><strong>{result.status==='success'?'Query complete':result.status==='refused'?'Request declined':'Could not run query'}</strong></div><span>{result.latency_ms!=null?`${result.latency_ms.toFixed(0)} ms total · `:''}{result.duration_ms.toFixed(0)} ms query/profile · {result.correction_attempts} correction{result.correction_attempts===1?'':'s'}</span></div><p className="summary">{result.summary}</p>
      {result.status==='success' && (result.response_type==='summary'||result.response_type==='examine' ? <>{result.response_type==='examine'&&result.statistics&&<ExamineReport statistics={result.statistics}/>}<details className="sql"><summary>Inspect raw profiling data</summary><pre>{JSON.stringify(result.statistics,null,2)}</pre></details></> : <><details className="sql"><summary>Inspect SQL <span>Read-only query</span></summary><pre>{result.sql}</pre></details>
        {chart?.type ? chartValid ? <div className="chart"><h3>{chart.type[0].toUpperCase()+chart.type.slice(1)} · {chart.series_columns.join(', ')} by {chart.label_column || chart.x_column}{chart.omitted_rows ? ` · ${chart.omitted_rows} incomplete rows omitted`:''}</h3>{renderChart(true)}</div> : <div className="empty">{chart.reason || 'This chart configuration does not match the result data.'} {chart.alternative || 'Inspect the table below.'}</div> : chart?.reason ? <div className="empty">{chart.reason} {chart.alternative}</div> : null}
        {result.rows.length>0&&<div className="chart-types"><span>Chart type</span>{(['bar','line','pie','donut','scatter'] as const).map(kind=><button key={kind} aria-pressed={chart?.type===kind} onClick={()=>changeChart(kind)}>{kind}</button>)}</div>}
        <div className="table-wrap"><table><thead><tr>{result.columns.map(c=><th key={c}>{c.replaceAll('_',' ')}</th>)}</tr></thead><tbody>{result.rows.map((row,i)=><tr key={i}>{result.columns.map(c=><td key={c}>{row[c]===null?'—':String(row[c])}</td>)}</tr>)}</tbody></table>{!result.rows.length&&<div className="empty">No matching rows.</div>}</div></>)}
    </section>}
    <section className="schema"><button className="schema-toggle" aria-expanded={showSchema} onClick={()=>setShowSchema(!showSchema)}>{showSchema?'−':'+'} Explore selected data</button>{showSchema&&<div className="schema-content">{selectedSchemas.map(source=><div key={source.id} className="source-schema"><b>{source.name} · {source.schema.source_type?.toUpperCase()||'SQLITE'}</b>{source.schema.revenue_definition&&<p>{source.schema.revenue_definition}</p>}<div className="schema-grid">{[...Object.entries(source.schema.tables).map(([name,meta])=>({name,meta,kind:'Table'})),...Object.entries(source.schema.views||{}).map(([name,meta])=>({name,meta,kind:'View'}))].map(({name,meta,kind})=><div key={`${source.id}-${kind}-${name}`}><b>{kind} · {name}{source.schema.source_names?.[name]&&source.schema.source_names[name]!==name?` · from ${source.schema.source_names[name]}`:''}</b><span>{Object.entries(meta.columns).map(([column,col])=>`${column} · ${col.type||'no type'}${col.primary_key?' · PK':''}${!col.nullable?' · required':''}`).join('  /  ')}{meta.foreign_keys.length>0&&` · References ${meta.foreign_keys.map(f=>`${f.column} → ${f.referenced_table}.${f.referenced_column}`).join(', ')}`}</span></div>)}</div></div>)}</div>}</section>
    <footer>QueryPilot · Natural language analytics with SQL you can inspect.</footer>
  </main>
}

function ExamineReport({statistics}:{statistics:Record<string,unknown>}) {
  const overview=statistics.overview as Record<string,number>|undefined
  const tables=statistics.tables as Record<string,{row_count:number;column_count:number;kind:string;duplicate_row_count:number|null;columns:Record<string,Record<string,unknown>>}>|undefined
  if(!overview||!tables)return <div className="profile-report"><h3>Data examination</h3><p>Profile statistics are unavailable.</p></div>
  return <div className="profile-report"><h3>Data examination</h3><div className="profile-metrics"><div><b>{overview.row_count.toLocaleString()}</b><span>table rows · {Number(overview.view_row_count||0).toLocaleString()} view rows</span></div><div><b>{overview.column_count.toLocaleString()}</b><span>columns</span></div><div><b>{overview.missing_value_count.toLocaleString()}</b><span>missing values · {overview.columns_with_missing} columns</span></div><div><b>{overview.duplicate_row_count.toLocaleString()}</b><span>duplicate rows</span></div></div>
    {Object.entries(tables).map(([table,info])=><details className="profile-table" key={table}><summary>{table} <span>{info.kind} · {info.row_count.toLocaleString()} rows · {info.column_count} columns</span></summary><div className="table-wrap"><table><thead><tr><th>Column</th><th>Missing</th><th>Distinct</th><th>Range / summary</th><th>Most common values</th></tr></thead><tbody>{Object.entries(info.columns).map(([name,stats])=><tr key={name}><td>{name} <small>{String(stats.type||'')}</small></td><td>{String(stats.missing_count??0)}</td><td>{stats.distinct_count==null?'—':String(stats.distinct_count)}</td><td>{stats.minimum!=null||stats.maximum!=null?`${stats.minimum??'—'} to ${stats.maximum??'—'}`:stats.average!=null?`avg ${stats.average} · sum ${stats.sum}`:'—'}</td><td>{Array.isArray(stats.top_values)?(stats.top_values as {value:unknown;count:number}[]).map(v=>`${String(v.value)} (${v.count})`).join(', ')||'—':'—'}</td></tr>)}</tbody></table></div>{info.duplicate_row_count!=null&&<p>{info.duplicate_row_count.toLocaleString()} duplicate rows</p>}</details>)}
    {Array.isArray(statistics.limitations)&&statistics.limitations.length>0&&<p className="profile-limits">{(statistics.limitations as string[]).join(' ')}</p>}{Boolean(statistics.timings_ms)&&<small className="profile-timing">Profiling completed in {Number((statistics.timings_ms as Record<string,number>).profiling).toFixed(1)} ms</small>}
  </div>
}
