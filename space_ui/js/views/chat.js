/* Chat tab — talks to the Plane-B chat endpoints (chat exclusion reversed by
   user decision 2026-07-15). Works identically across claude_code / hermes /
   openclaw because the broker exposes one wire contract:

     POST /api/chat/prompt  {text, session_id?, agent_id?} → {stream_id, session_id}
     GET  /api/chat/stream/{id}  SSE: session-created · text-delta ·
          model-loading · heartbeat · agent-error · error · done
     POST /api/chat/abort   {stream_id}
     GET  /api/sessions · /api/sessions/search · /api/messages/{id}

   Per-agent quirks handled here:
   - hermes: a NEW session's returned UUID can't fetch messages (state.db is
     keyed by the native id) — on done, if the transcript comes back empty we
     re-list sessions and adopt the newest id (reconcile).
   - openclaw: new sessions fake-stream buffered text in chunks — renders as
     bursty deltas, no special handling.
   - No agent emits live tool/permission events on this backend; tool calls
     appear in the stored transcript after done. Unknown SSE events are
     ignored, so richer backends light up without breaking this view.
   Refresh mid-stream is unrecoverable by design (no token replay, /api/chat/
   active is a stub): the transcript reloads from the DB with partial text. */
import {API_BASE,apiFetch,withPageQuery} from '../core/api.js';
import {mdToHtml} from '../core/markdown.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function rel(iso){
  if(!iso)return'';
  const t=typeof iso==='number'?iso:new Date(iso).getTime();
  const s=(Date.now()-t)/1000;
  if(!isFinite(s))return'';
  if(s<60)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
const trunc=(s,n)=>{s=String(s??'');return s.length>n?s.slice(0,n)+'\n… ('+(s.length-n)+' more chars)':s;};
const asText=v=>typeof v==='string'?v:JSON.stringify(v,null,2);

let root=null,logEl=null,sessEl=null,inputEl=null,sendBtn=null,stopBtn=null,projEl=null,statusEl=null;
let currentId=null;
let stream=null; /* {id, es, liveEl, text, newSession, lastEvent, watchdog} */
let searchT=null;

export default {
  id:'chat',label:'Chat',order:6,
  async mount(el){
    root=el;
    el.innerHTML=
      '<div class="chat">'
      +'<aside class="chat-side">'
        +'<div class="chat-side-head"><button id="chat-new">+ New chat</button></div>'
        +'<input id="chat-search" placeholder="Search sessions…" autocomplete="off" spellcheck="false">'
        +'<div class="chat-sess" id="chat-sess"><div class="chat-note">loading sessions…</div></div>'
      +'</aside>'
      +'<section class="chat-main">'
        +'<div class="chat-log" id="chat-log">'+emptyHint()+'</div>'
        +'<div class="chat-status" id="chat-status" hidden></div>'
        +'<form class="chat-form" id="chat-form">'
          +'<select id="chat-proj" title="Bind a NEW session to a project (agent works in its folder)"><option value="">no project</option></select>'
          +'<textarea id="chat-input" rows="2" placeholder="Message the agent… (Enter to send, Shift+Enter for a new line)"></textarea>'
          +'<div class="chat-btns">'
            +'<button id="chat-send" type="submit">Send</button>'
            +'<button id="chat-stop" type="button" hidden>Stop</button>'
          +'</div>'
        +'</form>'
      +'</section></div>';
    logEl=el.querySelector('#chat-log');sessEl=el.querySelector('#chat-sess');
    inputEl=el.querySelector('#chat-input');sendBtn=el.querySelector('#chat-send');
    stopBtn=el.querySelector('#chat-stop');projEl=el.querySelector('#chat-proj');
    statusEl=el.querySelector('#chat-status');
    el.querySelector('#chat-form').addEventListener('submit',e=>{e.preventDefault();send();});
    stopBtn.addEventListener('click',stop);
    el.querySelector('#chat-new').addEventListener('click',newChat);
    inputEl.addEventListener('keydown',e=>{
      if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}
    });
    el.querySelector('#chat-search').addEventListener('input',e=>{
      clearTimeout(searchT);
      const q=e.target.value.trim();
      searchT=setTimeout(()=>q.length>=2?searchSessions(q):loadSessions(),300);
    });
    loadSessions();
    loadProjects();
  }
};

function emptyHint(){
  return'<div class="chat-note">Pick a session on the left, or start a new chat. '
    +'New sessions can be bound to an xo-project below — the agent then works inside that project’s folder.</div>';
}

/* ---- sessions sidebar ---- */
async function loadSessions(){
  const res=await apiFetch(API_BASE+'/api/sessions?limit=50&offset=0');
  if(!res.ok){sessEl.innerHTML='<div class="chat-note">'+esc(res.error)+'</div>';return;}
  renderSessions(normalizeSessions(res.data));
}
async function searchSessions(q){
  const res=await apiFetch(API_BASE+'/api/sessions/search?q='+encodeURIComponent(q)+'&limit=20&offset=0');
  if(!res.ok){sessEl.innerHTML='<div class="chat-note">'+esc(res.error)+'</div>';return;}
  const hits=(Array.isArray(res.data)?res.data:[]).map(h=>h.session||h);
  renderSessions(hits);
}
function normalizeSessions(d){
  if(Array.isArray(d))return d;
  return d?.sessions||d?.items||[];
}
function renderSessions(list){
  if(!list.length){sessEl.innerHTML='<div class="chat-note">no sessions yet</div>';return;}
  sessEl.innerHTML=list.map(s=>'<button class="chat-si'+(s.id===currentId?' is-on':'')+'" data-id="'+esc(s.id)+'">'
    +'<span class="t">'+esc(s.title||'Untitled')+'</span>'
    +'<span class="m">'+esc(s.agent||'')+(s.time_updated?' · '+rel(s.time_updated):'')+'</span>'
    +'</button>').join('');
  sessEl.querySelectorAll('.chat-si').forEach(b=>b.addEventListener('click',()=>selectSession(b.dataset.id)));
}
async function selectSession(id){
  currentId=id;
  sessEl.querySelectorAll('.chat-si').forEach(b=>b.classList.toggle('is-on',b.dataset.id===id));
  await loadMessages(id);
}
function newChat(){
  currentId=null;
  sessEl.querySelectorAll('.chat-si').forEach(b=>b.classList.remove('is-on'));
  logEl.innerHTML=emptyHint();
  inputEl.focus();
}

/* ---- projects dropdown (agent_id binding for NEW sessions) ---- */
async function loadProjects(){
  const res=await apiFetch(API_BASE+'/api/xo-projects');
  if(!res.ok)return; /* dropdown just stays at "no project" */
  for(const p of res.data.items||[]){
    const o=document.createElement('option');
    o.value=p.id;o.textContent=p.display_name||p.id;
    projEl.appendChild(o);
  }
}

/* ---- transcript ---- */
async function loadMessages(id){
  logEl.innerHTML='<div class="chat-note">loading transcript…</div>';
  const res=await apiFetch(API_BASE+'/api/messages/'+encodeURIComponent(id)+'?limit=50&offset=-1');
  if(!res.ok){logEl.innerHTML='<div class="chat-note">'+esc(res.error)+'</div>';return res;}
  renderTranscript(res.data.messages||[]);
  return res;
}
function renderTranscript(messages){
  const out=[];
  for(const m of messages){
    const role=m.data?.role;
    const parts=(m.parts||[]).map(p=>p.data).filter(Boolean);
    if(role==='user'){
      const text=parts.filter(p=>p.type==='text').map(p=>p.text).join('\n');
      if(text)out.push('<div class="msg msg-user">'+esc(text)+'</div>');
    }else if(role==='assistant'){
      const inner=[];
      for(const p of parts){
        if(p.type==='text'&&p.text)inner.push('<div class="md">'+mdToHtml(p.text)+'</div>');
        else if(p.type==='reasoning'&&p.text)inner.push(
          '<details class="msg-reason"><summary>reasoning</summary><div class="md">'+mdToHtml(p.text)+'</div></details>');
        else if(p.type==='tool')inner.push(toolHTML(p));
      }
      if(inner.length)out.push('<div class="msg msg-ai">'+inner.join('')+'</div>');
    }
  }
  logEl.innerHTML=out.length?out.join(''):'<div class="chat-note">no messages in this session yet</div>';
  logEl.scrollTop=logEl.scrollHeight;
}
function toolHTML(p){
  const st=p.state||{};
  const status=st.status||'unknown';
  return'<details class="msg-tool st-'+esc(status)+'">'
    +'<summary><span class="tdot"></span>'+esc(p.tool||'tool')
    +(st.title?' · '+esc(st.title):'')+' <span class="tst">'+esc(status)+'</span></summary>'
    +(st.input!==undefined?'<div class="tlab">input</div><pre>'+esc(trunc(asText(st.input),2000))+'</pre>':'')
    +(st.output!==undefined?'<div class="tlab">output</div><pre>'+esc(trunc(asText(st.output),4000))+'</pre>':'')
    +'</details>';
}

/* ---- send / stream ---- */
async function send(){
  const text=inputEl.value.trim();
  if(!text||stream)return;
  const body={text};
  const newSession=!currentId;
  if(currentId)body.session_id=currentId;
  else if(projEl.value)body.agent_id=projEl.value;
  setBusy(true);
  const res=await apiFetch(API_BASE+'/api/chat/prompt',{method:'POST',body});
  if(!res.ok){setBusy(false);note('could not send: '+res.error);return;}
  inputEl.value='';
  currentId=res.data.session_id||currentId;
  if(logEl.querySelector('.chat-note'))logEl.innerHTML='';
  logEl.insertAdjacentHTML('beforeend','<div class="msg msg-user">'+esc(text)+'</div>');
  const live=document.createElement('div');
  live.className='msg msg-ai is-live';
  live.textContent='';
  logEl.appendChild(live);
  logEl.scrollTop=logEl.scrollHeight;
  openStream(res.data.stream_id,newSession,live);
}
function openStream(streamId,newSession,liveEl){
  const es=new EventSource(withPageQuery(API_BASE+'/api/chat/stream/'+streamId));
  stream={id:streamId,es,liveEl,text:'',newSession,lastEvent:Date.now(),watchdog:null};
  const touch=()=>{if(stream)stream.lastEvent=Date.now();};
  const data=e=>{try{return JSON.parse(e.data);}catch(err){return{};}};
  es.addEventListener('session-created',e=>{touch();const d=data(e);if(d.session_id)currentId=d.session_id;});
  es.addEventListener('text-delta',e=>{
    touch();hideStatus();
    const d=data(e);
    if(!stream||!d.text)return;
    stream.text+=d.text;
    stream.liveEl.textContent=stream.text;
    pinScroll();
  });
  es.addEventListener('model-loading',e=>{touch();showStatus(data(e).label||'working…');});
  es.addEventListener('heartbeat',touch);
  es.addEventListener('agent-error',e=>{touch();finishStream(data(e).error_message||'agent error');});
  es.addEventListener('error',e=>{if(e.data){touch();finishStream(data(e).error_message||'stream error');}});
  es.addEventListener('done',e=>{
    touch();
    const d=data(e);
    if(d.session_id)currentId=d.session_id;
    finishStream(null);
  });
  /* watchdog: >45s with no event (heartbeats come every 15-20s) = dead stream */
  stream.watchdog=setInterval(()=>{
    if(stream&&Date.now()-stream.lastEvent>45000)finishStream('stream timed out — showing what was saved');
  },10000);
}
async function finishStream(errMsg){
  if(!stream)return;
  clearInterval(stream.watchdog);
  stream.es.close();
  const wasNew=stream.newSession;
  if(errMsg)stream.liveEl.insertAdjacentHTML('beforeend','<div class="chat-err">'+esc(errMsg)+'</div>');
  stream=null;
  hideStatus();setBusy(false);
  await refetchAfterTurn(wasNew);
}
async function refetchAfterTurn(wasNew){
  if(!currentId){loadSessions();return;}
  let res=await loadMessages(currentId);
  /* hermes reconcile: a brand-new session's returned UUID may not resolve in
     the message store — adopt the newest session id and retry once */
  if(wasNew&&res&&res.ok&&!(res.data.messages||[]).length){
    const sl=await apiFetch(API_BASE+'/api/sessions?limit=5&offset=0');
    const list=sl.ok?normalizeSessions(sl.data):[];
    if(list.length){currentId=list[0].id;await loadMessages(currentId);}
  }
  loadSessions();
}
async function stop(){
  if(!stream)return;
  apiFetch(API_BASE+'/api/chat/abort',{method:'POST',body:{stream_id:stream.id}});
  finishStream('stopped — the agent may still finish server-side; the transcript below is what was saved');
}

/* ---- small ui helpers ---- */
function setBusy(b){
  sendBtn.disabled=b;
  stopBtn.hidden=!b;
  projEl.disabled=b;
}
function showStatus(t){statusEl.textContent=t;statusEl.hidden=false;}
function hideStatus(){statusEl.hidden=true;}
function note(t){logEl.insertAdjacentHTML('beforeend','<div class="chat-err">'+esc(t)+'</div>');pinScroll();}
function pinScroll(){
  if(logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight<120)logEl.scrollTop=logEl.scrollHeight;
}
