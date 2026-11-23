import { DatabaseSync } from 'node:sqlite';
import fs from 'node:fs';
import path from 'node:path';

// Commitment: record each observed hook event by the planner's actual run ID, without affecting game control.
// Assumption: a single live Pi process owns the prompt clock and writes its SQLite journal.
// Trade-off: synchronous writes and an unbounded, indexed journal consume disk; telemetry errors are reported separately.
// Reconsider: write latency, journal growth, changed hook ordering, or concurrent planner ownership.
export function createFlowTrace({root=process.env.GAMEAGENT_HOME||path.resolve('data'),clockPath=process.env.GAMEAGENT_PROMPT_CLOCK||path.join(root,'round.json'),traceDir=process.env.GAMEAGENT_TRACE_DIR||path.join(root,'flow-trace'),now=()=>Date.now()/1000}={}){
 let db=null,error=null;const dir=traceDir,dbPath=path.join(dir,'events.sqlite3'),errorPath=path.join(dir,'error.json');let errorReported=fs.existsSync(errorPath);
 try{fs.mkdirSync(dir,{recursive:true,mode:0o750});db=new DatabaseSync(dbPath);fs.chmodSync(dbPath,0o640);db.exec('PRAGMA busy_timeout=250; PRAGMA journal_mode=DELETE; CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL, phase TEXT NOT NULL, event_at REAL NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL); CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq); CREATE INDEX IF NOT EXISTS events_time ON events(event_at,seq);')}catch(e){error=String(e?.message||e)}
 const markError=e=>{error=String(e?.message||e);errorReported=true;try{fs.writeFileSync(errorPath,JSON.stringify({at:now(),message:error}),{mode:0o640});fs.chmodSync(errorPath,0o640)}catch{}};
 function clean(value){return JSON.parse(JSON.stringify(value??null,(k,v)=>{
  if(typeof k==='string'&&/^(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)$/i.test(k))return '[已隐藏凭据]';
  if(/^(reasoning|reasoning_content|analysis|chain_of_thought)$/i.test(k))return '[不展示内部推理]';
  if(v&&typeof v==='object'&&['thinking','reasoning','redacted_thinking'].includes(v.type))return {type:v.type,content:'[不展示内部推理]'};
  if(typeof v==='string')return v.replace(/\bsk-[A-Za-z0-9_-]{16,}\b/g,'[已隐藏凭据]');
  return v
 }))}
 function runId(){try{const id=JSON.parse(fs.readFileSync(clockPath,'utf8')).run_id;return typeof id==='string'&&/^[0-9a-f]{32}$/.test(id)?id:null}catch{return null}}
 function write(phase,summary={},detail=null,id=runId(),at=now()){
  if(!db){if(error)markError(error);return false}if(!id){markError('轮次标记不可用');return false}
  try{const cleanSummary=clean(summary),cleanDetail=clean(detail);db.prepare('INSERT INTO events(run_id,phase,event_at,summary,detail) VALUES(?,?,?,?,?)').run(id,phase,at,JSON.stringify(cleanSummary),JSON.stringify(cleanDetail));if(errorReported){try{fs.unlinkSync(errorPath);errorReported=false;error=null}catch{}}return true}catch(e){markError(e);return false}
 }
 return {write,runId,close:()=>{try{db?.close()}catch{}},getError:()=>error};
}
