#!/usr/bin/env python3
"""Local, read-only monitor for the standalone Agent."""
import argparse,html,importlib.util,json,os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
P=Path(__file__).resolve().parent;home=Path(os.environ.get('GAMEAGENT_HOME',P/'data')).resolve()
if not home.is_relative_to(P):raise SystemExit('数据目录必须位于独立项目内')
os.environ['GAMEAGENT_TRACE_DIR']=str(home/'flow-trace')
spec=importlib.util.spec_from_file_location('agent_flow_read',P/'runtime'/'agent_flow_read.py');reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)
PAGE='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Agent 监控</title><style>body{font:15px system-ui;background:#102028;color:#e4f4ef;max-width:950px;margin:30px auto;padding:0 18px}h1{font-size:24px}section{background:#183139;border-radius:10px;padding:15px;margin:12px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}button{padding:7px;background:#285044;color:white;border:0;border-radius:5px;cursor:pointer}.event{padding:7px;border-bottom:1px solid #35545b}</style><h1>独立 Agent 监控</h1><p>仅显示本项目的状态和逐轮记录。</p><section><b>服务状态</b><pre id="state">读取中</pre></section><section><b>最近事件</b><div id="events">读取中</div></section><section><b>历史轮次</b><div id="history"></div><button id="more" onclick="loadHistory()">加载历史</button></section><section><b>轮次详情</b><pre id="detail">点击事件查看该轮</pre></section><script>let cursor=null,done=false;async function loadHistory(){if(done)return;let x=await fetch("/api/history"+(cursor?"?before="+cursor:"")).then(r=>r.json());for(const row of x.rounds||[]){let b=document.createElement("button");b.textContent=new Date(row.started_at*1000).toLocaleString()+" · "+row.run_id.slice(0,8);b.onclick=async()=>{let r=await fetch("/api/round?run_id="+encodeURIComponent(row.run_id));document.querySelector("#detail").textContent=JSON.stringify(await r.json(),null,2)};document.querySelector("#history").append(b)}cursor=x.next_before;done=!cursor;if(done)document.querySelector("#more").textContent="已到最早记录"}async function refresh(){try{let [s,e]=await Promise.all([fetch('/api/status').then(r=>r.json()),fetch('/api/events').then(r=>r.json())]);document.querySelector('#state').textContent=JSON.stringify(s,null,2);let root=document.querySelector('#events');root.replaceChildren();for(const x of (e.events||[]).slice(0,20)){let d=document.createElement('div'),b=document.createElement('button');d.className='event';b.textContent=new Date(x.at*1000).toLocaleTimeString()+' · '+x.phase+' · '+x.run_id.slice(0,8);b.onclick=async()=>{let r=await fetch('/api/round?run_id='+encodeURIComponent(x.run_id));document.querySelector('#detail').textContent=JSON.stringify(await r.json(),null,2)};d.append(b);root.append(d)}if(!root.children.length)root.textContent='暂无事件'}catch(e){document.querySelector('#events').textContent='读取失败'}}refresh();setInterval(refresh,2000);</script></html>'''
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  url=urlsplit(self.path);code=200;kind='application/json; charset=utf-8'
  if url.path=='/':body=PAGE.encode();kind='text/html; charset=utf-8'
  else:
   if url.path=='/api/status':
    try:data=json.loads((home/'status.json').read_text())
    except (OSError,ValueError):data={'status':'not_running'}
   elif url.path=='/api/events':data=reader.recent()
   elif url.path=='/api/history':
    try:data=reader.history(parse_qs(url.query).get('before',[None])[0])
    except ValueError:data={'status':'invalid_cursor'};code=400
   elif url.path=='/api/round':
    try:data=reader.one_round(parse_qs(url.query).get('run_id',[''])[0])
    except ValueError:data={'status':'invalid_run_id'};code=400
   else:data={'error':'not_found'};code=404
   body=json.dumps(data,ensure_ascii=False).encode()
  self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=0);a=p.parse_args();server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler);print('http://127.0.0.1:'+str(server.server_port)+'/',flush=True);server.serve_forever()
