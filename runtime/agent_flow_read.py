"""Read-only per-round Agent hook records for the dashboard.

Commitment: return recorded events without inferring missing phases or disclosing credentials.
Assumption: the fenced Pi extension is the sole writer and timestamps use the host clock.
Trade-off: historical full inputs consume disk and can be read only by round ID.
Reconsider: journal growth, multi-writer contention, changed event schema or input sensitivity.
"""
import json,os,re,sqlite3,time
from pathlib import Path
ROOT=Path(os.environ.get('GAMEAGENT_TRACE_DIR',str(Path(__file__).resolve().parent.parent/'data'/'flow-trace')))
DB=ROOT/'events.sqlite3'

def _error():
 try:
  d=json.loads((ROOT/'error.json').read_text());return {'at':d.get('at'),'message':'记录写入失败，请检查服务器日志'}
 except (OSError,ValueError,TypeError):return None

def _public(value):
 if isinstance(value,list):return [_public(x) for x in value]
 if isinstance(value,dict):
  if value.get('type') in ('thinking','reasoning','redacted_thinking'):return {'type':value.get('type'),'content':'[不展示内部推理]'}
  return {k:('[不展示内部推理]' if k.lower() in ('reasoning','reasoning_content','analysis','chain_of_thought') else _public(v)) for k,v in value.items()}
 return value

def _connect():return sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)
def recent(now=None):
 now=time.time() if now is None else now
 try:
  if not DB.exists():return {'status':'not_started','events':[],'error':_error()}
  with _connect() as con:
   rows=con.execute('SELECT seq,run_id,phase,event_at,summary FROM events WHERE event_at>=? ORDER BY seq DESC LIMIT 600',(now-120,)).fetchall()
  events=[{'seq':seq,'run_id':rid,'phase':phase,'at':at,'summary':json.loads(summary)} for seq,rid,phase,at,summary in rows]
  return {'status':'ok','updated_at':now,'events':events,'error':_error()}
 except (OSError,sqlite3.Error,ValueError,TypeError):return {'status':'error','events':[],'error':{'message':'记录读取失败'}}
def one_round(run_id):
 if not isinstance(run_id,str) or not re.fullmatch(r'[0-9a-f]{32}',run_id):raise ValueError('invalid run ID')
 try:
  if not DB.exists():return {'status':'not_started','run_id':run_id,'events':[]}
  with _connect() as con:
   rows=con.execute('SELECT seq,phase,event_at,summary,detail FROM events WHERE run_id=? ORDER BY seq',(run_id,)).fetchall()
  return {'status':'ok' if rows else 'not_found','run_id':run_id,'events':[{'seq':seq,'phase':phase,'at':at,'summary':json.loads(summary),'detail':_public(json.loads(detail))} for seq,phase,at,summary,detail in rows]}
 except (OSError,sqlite3.Error,ValueError,TypeError):return {'status':'error','run_id':run_id,'events':[]}

def history(before=None):
 if before is not None:
  try:before=int(before)
  except (ValueError,TypeError):raise ValueError('invalid cursor')
  if before<=0:raise ValueError('invalid cursor')
 try:
  if not DB.exists():return {'status':'not_started','rounds':[],'next_before':None}
  with _connect() as con:
   rows=con.execute("SELECT seq,run_id,event_at FROM events WHERE phase='prefetch_start' AND seq<? ORDER BY seq DESC LIMIT 21",(before if before is not None else 9223372036854775807,)).fetchall()
  page=rows[:20]
  return {'status':'ok','rounds':[{'seq':seq,'run_id':rid,'started_at':at} for seq,rid,at in page],'next_before':page[-1][0] if len(rows)>20 and page else None}
 except (OSError,sqlite3.Error,ValueError,TypeError):return {'status':'error','rounds':[],'next_before':None}
