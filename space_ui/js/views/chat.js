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
import experimentPanel from './experiment.js';

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
let studioEl=null,chatModeToggle=null,experimentModeToggle=null,studioContextTitle=null,studioContextDetail=null;
let chatSideEl=null,chatCenterEl=null,chatMainEl=null,experimentMainEl=null,experimentSideEl=null,experimentRailToggle=null,experimentScrim=null;
let chatNewBtn=null,sessionCountEl=null,chatTitleEl=null,chatSubtitleEl=null,chatPresenceEl=null;
let chatSideToggle=null,chatSideScrim=null;
let currentId=null;
let stream=null; /* {id, es, liveEl, text, newSession, lastEvent, watchdog} */
let searchT=null;
let experimentMounted=false,drawerOpen=false,sessionDrawerOpen=false,chatVisible=false,pendingExperimentOpen=false,workspaceMode='chat';
const drawerMedia=matchMedia('(max-width:980px)');
const sessionDrawerMedia=matchMedia('(max-width:760px)');

export default {
  id:'chat',label:'Chat',order:6,
  mount(el){
    root=el;
    el.innerHTML=
      '<div class="agent-studio" data-workspace-mode="chat">'
      +'<header class="studio-bar">'
        +'<div class="studio-identity"><span class="studio-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6 5.6 18.4"/><circle cx="12" cy="12" r="3.2"/></svg></span>'
          +'<div><span>Agent studio</span><strong id="studio-mode-copy">Workspace chat</strong></div></div>'
        +'<div class="studio-switch" role="group" aria-label="Agent Studio workspace">'
          +'<button id="chat-mode-chat" type="button" aria-pressed="true" aria-controls="chat-side chat-main"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4.5h12v8H9l-3.5 3v-3H4z"/></svg><span>Chat</span></button>'
          +'<button id="chat-mode-experiment" type="button" aria-pressed="false" aria-controls="chat-exp-side chat-experiment-main"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6 3v5l-3 6.5A1.7 1.7 0 0 0 4.5 17h11a1.7 1.7 0 0 0 1.5-2.5L14 8V3M5 11h10M5 3h10"/></svg><span>Experiments</span></button>'
        +'</div>'
        +'<div class="studio-context"><span class="studio-context-copy"><i></i><span><b id="studio-context-title">Workspace ready</b><small id="studio-context-detail">XO agent connected</small></span></span>'
          +'<button id="chat-exp-rail-toggle" class="studio-rail-toggle" type="button" aria-controls="chat-exp-side" aria-expanded="false" aria-label="Open launcher and runs" hidden><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 5h12M4 10h12M4 15h12"/></svg><span>Launcher &amp; runs</span></button></div>'
      +'</header>'
      +'<div class="chat">'
      +'<aside class="chat-side" id="chat-side" aria-label="Conversations">'
        +'<div class="chat-side-head"><div class="chat-side-title"><span>Workspace</span><h2>Conversations</h2></div><div class="chat-side-actions">'
          +'<button id="chat-new" type="button" aria-label="Start a new chat"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg><span>New</span></button>'
          +'<button id="chat-side-close" class="chat-side-close" type="button" aria-label="Close Conversations"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15"/></svg></button></div></div>'
        +'<div class="chat-search-wrap"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></svg>'
          +'<label class="sr-only" for="chat-search">Search conversations</label><input id="chat-search" type="search" placeholder="Search conversations" autocomplete="off" spellcheck="false"></div>'
        +'<div class="chat-side-label"><span>Recent</span><span id="chat-session-count"></span></div>'
        +'<div class="chat-sess" id="chat-sess"><div class="chat-note">loading sessions…</div></div>'
        +'<div class="chat-side-foot"><span class="chat-side-status"><i></i>XO agent online</span><span>Enter to send</span></div>'
      +'</aside>'
      +'<button id="chat-side-scrim" class="chat-side-scrim" type="button" tabindex="-1" aria-label="Close Conversations" hidden></button>'
      +'<div class="chat-center">'
        +'<section class="chat-main" id="chat-main">'
          +'<header class="chat-head"><div class="chat-head-start">'
            +'<button id="chat-side-toggle" class="chat-side-toggle" type="button" aria-controls="chat-side" aria-expanded="false" aria-label="Open Conversations" hidden><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 5h12M4 10h12M4 15h8"/></svg></button>'
            +'<div class="chat-head-copy"><span>Workspace agent</span><h1 id="chat-title">New conversation</h1><p id="chat-subtitle">Explore, build, and run work across XO.</p></div></div>'
            +'<div class="chat-head-actions"><span id="chat-presence" class="chat-presence"><i></i><span>Ready</span></span></div>'
          +'</header>'
          +'<div class="chat-log" id="chat-log" role="log" aria-live="polite" aria-relevant="additions text" aria-label="Conversation">'+emptyHint()+'</div>'
          +'<div class="chat-status" id="chat-status" role="status" aria-live="polite" hidden></div>'
          +'<div class="chat-compose-shell"><form class="chat-form" id="chat-form">'
            +'<label class="sr-only" for="chat-input">Message the workspace agent</label><textarea id="chat-input" rows="2" placeholder="Ask anything, make a change, or run a task…"></textarea>'
            +'<div class="chat-form-foot"><label class="chat-project-control" for="chat-proj"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3.5 6.5h5l1.5 2h6.5v7.5h-13z"/><path d="M3.5 6.5V4h5l1.5 2h6.5v2.5"/></svg><span>Context</span>'
              +'<select id="chat-proj" title="Bind a NEW session to a project (agent works in its folder)"><option value="">Entire workspace</option></select></label>'
              +'<span class="chat-compose-hint">Enter to send · Shift+Enter for a new line</span><div class="chat-btns">'
              +'<button id="chat-stop" type="button" aria-label="Stop the current response" hidden>Stop</button>'
              +'<button id="chat-send" type="submit"><span>Send</span><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 10 10-6-3 12-2.5-4z"/><path d="m9.5 12 5.5-8"/></svg></button>'
            +'</div></div>'
          +'</form></div>'
        +'</section>'
        +'<section class="chat-experiment-main" id="chat-experiment-main" aria-label="Experiment workbench" hidden></section>'
      +'</div>'
      +'<button id="chat-exp-scrim" class="chat-exp-scrim" type="button" tabindex="-1" aria-label="Close Experiments" hidden></button>'
      +'<aside class="chat-exp-side" id="chat-exp-side" tabindex="-1" aria-label="Experiments">'
        +'<div id="chat-exp-panel" class="chat-exp-panel"></div>'
      +'</aside>'
      +'</div></div>';
    studioEl=el.querySelector('.agent-studio');
    chatModeToggle=el.querySelector('#chat-mode-chat');
    experimentModeToggle=el.querySelector('#chat-mode-experiment');
    studioContextTitle=el.querySelector('#studio-context-title');
    studioContextDetail=el.querySelector('#studio-context-detail');
    logEl=el.querySelector('#chat-log');sessEl=el.querySelector('#chat-sess');
    inputEl=el.querySelector('#chat-input');sendBtn=el.querySelector('#chat-send');
    stopBtn=el.querySelector('#chat-stop');projEl=el.querySelector('#chat-proj');
    statusEl=el.querySelector('#chat-status');
    chatNewBtn=el.querySelector('#chat-new');sessionCountEl=el.querySelector('#chat-session-count');
    chatTitleEl=el.querySelector('#chat-title');chatSubtitleEl=el.querySelector('#chat-subtitle');
    chatPresenceEl=el.querySelector('#chat-presence');
    chatSideEl=el.querySelector('.chat-side');
    chatCenterEl=el.querySelector('.chat-center');
    chatMainEl=el.querySelector('#chat-main');
    experimentMainEl=el.querySelector('#chat-experiment-main');
    experimentSideEl=el.querySelector('#chat-exp-side');
    experimentRailToggle=el.querySelector('#chat-exp-rail-toggle');
    experimentScrim=el.querySelector('#chat-exp-scrim');
    chatSideToggle=el.querySelector('#chat-side-toggle');
    chatSideScrim=el.querySelector('#chat-side-scrim');
    el.querySelector('#chat-form').addEventListener('submit',e=>{e.preventDefault();send();});
    stopBtn.addEventListener('click',stop);
    chatNewBtn.addEventListener('click',newChat);
    el.querySelector('#chat-side-close').addEventListener('click',()=>setSessionDrawerOpen(false,true));
    logEl.addEventListener('click',event=>{
      const starter=event.target.closest('[data-chat-starter]');
      if(!starter||stream)return;
      inputEl.value=starter.dataset.chatStarter||'';
      inputEl.focus();
    });
    inputEl.addEventListener('keydown',e=>{
      if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}
    });
    el.querySelector('#chat-search').addEventListener('input',e=>{
      clearTimeout(searchT);
      const q=e.target.value.trim();
      searchT=setTimeout(()=>q.length>=2?searchSessions(q):loadSessions(),300);
    });
    chatModeToggle.addEventListener('click',showChatConversation);
    experimentModeToggle.addEventListener('click',()=>openExperimentWorkspace({revealRail:true}));
    experimentRailToggle.addEventListener('click',()=>setDrawerOpen(!drawerOpen));
    experimentScrim.addEventListener('click',()=>setDrawerOpen(false,true));
    chatSideToggle.addEventListener('click',()=>setSessionDrawerOpen(!sessionDrawerOpen));
    chatSideScrim.addEventListener('click',()=>setSessionDrawerOpen(false,true));
    drawerMedia.addEventListener('change',syncDrawer);
    sessionDrawerMedia.addEventListener('change',syncSessionDrawer);
    el.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&drawerOpen){event.stopPropagation();setDrawerOpen(false,true);}
      else if(event.key==='Escape'&&sessionDrawerOpen){event.stopPropagation();setSessionDrawerOpen(false,true);}
    });
    addEventListener('space:open-experiment',requestExperimentOpen);
    syncWorkspaceLayout();
    loadSessions();
    loadProjects();
    experimentPanel.mount(el.querySelector('#chat-exp-panel'),{
        workbenchEl:experimentMainEl,
        activate:showExperimentConversation,
        deactivate:showChatConversation,
        openDrawer:()=>{
          setDrawerOpen(true);
          requestAnimationFrame(()=>experimentSideEl?.querySelector('#exp-project, #exp-launch')?.focus({preventScroll:true}));
        },
        closeDrawer:()=>setDrawerOpen(false,true),
      }).then(()=>{
        experimentMounted=true;
        if(pendingExperimentOpen&&workspaceMode==='experiment'){
          pendingExperimentOpen=false;
          experimentPanel.activateCurrent();
        }
        if(chatVisible)experimentPanel.show();
      }).catch(error=>{
        console.error('Experiment panel failed to mount:',error);
        el.querySelector('#chat-exp-panel').innerHTML='<div class="chat-exp-failed">'
          +'<b>Experiments unavailable</b><span>The Chat workspace is still available. Check the browser console for details.</span></div>';
      });
  },
  show(){
    chatVisible=true;
    if(experimentMounted)experimentPanel.show();
  },
  hide(){
    chatVisible=false;
    if(experimentMounted)experimentPanel.hide();
    setDrawerOpen(false);
    setSessionDrawerOpen(false);
  }
};

function emptyHint(){
  return'<div class="chat-empty"><div class="chat-empty-mark" aria-hidden="true"><span></span><svg viewBox="0 0 40 40"><path d="M20 7v26M7 20h26"/><path d="m11 11 18 18M29 11 11 29"/></svg></div>'
    +'<span class="chat-empty-kicker">XO workspace agent</span><h2>What are we working on?</h2>'
    +'<p>Ask a question, investigate a project, or give the agent a task. Choose a project context below when you want it to work inside that folder.</p>'
    +'<div class="chat-starters"><button type="button" data-chat-starter="Give me a concise overview of the active XO projects.">Summarize the workspace</button>'
    +'<button type="button" data-chat-starter="Inspect the selected project and suggest the highest-impact next task.">Find the next task</button>'
    +'<button type="button" data-chat-starter="Review the selected project for risks, broken flows, and missing tests.">Review a project</button></div></div>';
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
  sessionCountEl.textContent=list.length?String(list.length):'';
  if(!list.length){sessEl.innerHTML='<div class="chat-note chat-note-empty">No conversations yet.<br>Start one above.</div>';return;}
  sessEl.innerHTML=list.map(s=>{
    const title=s.title||'Untitled conversation';
    const meta=[s.agent||'',s.time_updated?rel(s.time_updated):''].filter(Boolean).join(' · ');
    const initial=title.trim().charAt(0).toUpperCase()||'X';
    return'<button class="chat-si'+(s.id===currentId?' is-on':'')+'" data-id="'+esc(s.id)+'"'
      +(s.id===currentId?' aria-current="true"':'')+(stream?' disabled':'')+'>'
      +'<span class="chat-si-mark" aria-hidden="true">'+esc(initial)+'</span><span class="chat-si-copy">'
      +'<span class="t">'+esc(title)+'</span><span class="m">'+esc(meta||'Workspace agent')+'</span></span>'
      +'<svg class="chat-si-arrow" viewBox="0 0 20 20" aria-hidden="true"><path d="m8 5 5 5-5 5"/></svg></button>';
  }).join('');
  sessEl.querySelectorAll('.chat-si').forEach(b=>b.addEventListener('click',()=>selectSession(b.dataset.id)));
}
async function selectSession(id){
  if(stream)return;
  showChatConversation();
  setSessionDrawerOpen(false);
  currentId=id;
  let selectedTitle='Conversation',selectedMeta='Workspace agent';
  sessEl.querySelectorAll('.chat-si').forEach(b=>{
    const selected=b.dataset.id===id;
    b.classList.toggle('is-on',selected);
    if(selected){
      b.setAttribute('aria-current','true');
      selectedTitle=b.querySelector('.t')?.textContent||selectedTitle;
      selectedMeta=b.querySelector('.m')?.textContent||selectedMeta;
    }else b.removeAttribute('aria-current');
  });
  updateConversationHeader(selectedTitle,selectedMeta);
  updateProjectControl();
  await loadMessages(id);
}
function newChat(){
  if(stream)return;
  showChatConversation();
  setSessionDrawerOpen(false);
  currentId=null;
  sessEl.querySelectorAll('.chat-si').forEach(b=>{b.classList.remove('is-on');b.removeAttribute('aria-current');});
  logEl.innerHTML=emptyHint();
  updateConversationHeader('New conversation','Explore, build, and run work across XO.');
  updateProjectControl();
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
  logEl.innerHTML=out.length?out.join(''):emptyHint();
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
  if(newSession)updateConversationHeader(text.length>52?text.slice(0,52)+'…':text,projEl.value?'Project-bound conversation':'Entire workspace');
  const res=await apiFetch(API_BASE+'/api/chat/prompt',{method:'POST',body});
  if(!res.ok){setBusy(false);note('could not send: '+res.error);return;}
  inputEl.value='';
  currentId=res.data.session_id||currentId;
  updateProjectControl();
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
  chatNewBtn.disabled=b;
  sessEl.querySelectorAll('.chat-si').forEach(button=>{button.disabled=b;});
  root?.setAttribute('aria-busy',String(b));
  root?.classList.toggle('is-streaming',b);
  updatePresence(b?'Agent working':'Ready',b);
  updateProjectControl();
}
function showStatus(t){statusEl.textContent=t;statusEl.hidden=false;updatePresence(t,true);}
function hideStatus(){statusEl.hidden=true;}
function note(t){logEl.insertAdjacentHTML('beforeend','<div class="chat-err" role="alert">'+esc(t)+'</div>');pinScroll();}
function pinScroll(){
  if(logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight<120)logEl.scrollTop=logEl.scrollHeight;
}

function updateConversationHeader(title,subtitle){
  if(chatTitleEl)chatTitleEl.textContent=title||'Conversation';
  if(chatSubtitleEl)chatSubtitleEl.textContent=subtitle||'Workspace agent';
}
function updatePresence(label,busy=false){
  if(chatPresenceEl){
    chatPresenceEl.classList.toggle('is-busy',busy);
    const text=chatPresenceEl.querySelector('span');
    if(text)text.textContent=label;
  }
  if(workspaceMode==='chat'&&studioContextTitle){
    studioContextTitle.textContent=busy?label:'Workspace ready';
    studioContextDetail.textContent=busy?'Conversation remains active':'XO agent connected';
  }
}
function updateProjectControl(){
  if(!projEl)return;
  projEl.disabled=Boolean(stream)||Boolean(currentId);
  projEl.closest('.chat-project-control')?.classList.toggle('is-locked',Boolean(currentId));
}

/* ---- shared Agent Studio modes + responsive context rails ---- */
function showChatConversation(){
  if(!chatMainEl||!experimentMainEl)return;
  const focusWasInExperiment=experimentMainEl.contains(document.activeElement)||experimentSideEl?.contains(document.activeElement);
  workspaceMode='chat';
  pendingExperimentOpen=false;
  drawerOpen=false;
  root?.classList.remove('is-experiment-open');
  experimentPanel.setActive(false);
  syncWorkspaceLayout();
  updatePresence(stream?'Agent working':'Ready',Boolean(stream));
  if(focusWasInExperiment)requestAnimationFrame(()=>chatModeToggle?.focus({preventScroll:true}));
}

function showExperimentConversation(){
  if(!chatMainEl||!experimentMainEl)return;
  const focusWasInChat=chatMainEl.contains(document.activeElement)||chatSideEl?.contains(document.activeElement);
  workspaceMode='experiment';
  sessionDrawerOpen=false;
  root?.classList.add('is-experiment-open');
  drawerOpen=false;
  syncWorkspaceLayout();
  if(focusWasInChat)requestAnimationFrame(()=>experimentModeToggle?.focus({preventScroll:true}));
}

function openExperimentWorkspace({revealRail=false}={}){
  showExperimentConversation();
  let activated=false;
  if(experimentMounted)activated=Boolean(experimentPanel.activateCurrent());
  else pendingExperimentOpen=true;
  if(revealRail&&drawerMedia.matches&&!activated)setDrawerOpen(true);
}

function requestExperimentOpen(){
  delete document.documentElement.dataset.openExperiment;
  openExperimentWorkspace();
}

function setDrawerOpen(open,returnFocus=false){
  const wasOpen=drawerOpen;
  drawerOpen=workspaceMode==='experiment'&&Boolean(open)&&drawerMedia.matches;
  if(drawerOpen)sessionDrawerOpen=false;
  syncWorkspaceLayout();
  if(drawerOpen&&!wasOpen){
    requestAnimationFrame(()=>{
      const first=experimentSideEl?.querySelector('button:not([disabled]),select:not([disabled]),input:not([disabled]),textarea:not([disabled]),a[href]');
      (first||experimentSideEl)?.focus({preventScroll:true});
    });
  }
  if(returnFocus)experimentRailToggle?.focus({preventScroll:true});
}

function setSessionDrawerOpen(open,returnFocus=false){
  const wasOpen=sessionDrawerOpen;
  sessionDrawerOpen=workspaceMode==='chat'&&Boolean(open)&&sessionDrawerMedia.matches;
  if(sessionDrawerOpen)drawerOpen=false;
  syncWorkspaceLayout();
  if(sessionDrawerOpen&&!wasOpen){
    requestAnimationFrame(()=>chatSideEl?.querySelector('button:not([disabled]),input:not([disabled])')?.focus({preventScroll:true}));
  }
  if(returnFocus)chatSideToggle?.focus({preventScroll:true});
}

function syncWorkspaceLayout(){
  if(!studioEl||!chatSideEl||!experimentSideEl||!chatCenterEl)return;
  const chatMode=workspaceMode==='chat';
  const chatCompact=sessionDrawerMedia.matches;
  const experimentCompact=drawerMedia.matches;
  if(!chatMode||!chatCompact)sessionDrawerOpen=false;
  if(chatMode||!experimentCompact)drawerOpen=false;

  const chatCovered=chatMode&&chatCompact&&sessionDrawerOpen;
  const experimentCovered=!chatMode&&experimentCompact&&drawerOpen;
  const showChatSide=chatMode&&(!chatCompact||sessionDrawerOpen);
  const showExperimentSide=!chatMode&&(!experimentCompact||drawerOpen);
  const focusInChatSide=chatSideEl.contains(document.activeElement);
  const focusInExperimentSide=experimentSideEl.contains(document.activeElement);

  studioEl.dataset.workspaceMode=workspaceMode;
  root.dataset.workspaceMode=workspaceMode;
  chatModeToggle.setAttribute('aria-pressed',String(chatMode));
  experimentModeToggle.setAttribute('aria-pressed',String(!chatMode));
  studioEl.querySelector('#studio-mode-copy').textContent=chatMode?'Workspace chat':'Self-hosted lab';
  if(chatMode){
    studioContextTitle.textContent=stream?'Agent working':'Workspace ready';
    studioContextDetail.textContent=stream?'Conversation remains active':'XO agent connected';
  }else{
    studioContextTitle.textContent='Agents API session';
    studioContextDetail.textContent='Isolated VPS workspace';
  }

  chatMainEl.hidden=!chatMode;
  experimentMainEl.hidden=chatMode;
  chatSideEl.hidden=!showChatSide;
  chatSideEl.inert=!showChatSide;
  experimentSideEl.hidden=!showExperimentSide;
  experimentSideEl.inert=!showExperimentSide;
  chatCenterEl.inert=chatCovered||experimentCovered;

  chatSideToggle.hidden=!chatMode||!chatCompact;
  chatSideToggle.setAttribute('aria-expanded',String(chatCovered));
  experimentRailToggle.hidden=chatMode||!experimentCompact;
  experimentRailToggle.setAttribute('aria-expanded',String(experimentCovered));
  chatSideScrim.hidden=!chatCovered;
  experimentScrim.hidden=!experimentCovered;
  root.classList.toggle('is-chat-drawer-open',chatCovered);
  root.classList.toggle('is-exp-drawer-open',experimentCovered);

  if(!showChatSide&&focusInChatSide)requestAnimationFrame(()=>(chatMode?chatSideToggle:chatModeToggle)?.focus({preventScroll:true}));
  if(!showExperimentSide&&focusInExperimentSide)requestAnimationFrame(()=>(chatMode?experimentModeToggle:experimentRailToggle)?.focus({preventScroll:true}));
}

function syncSessionDrawer(){syncWorkspaceLayout();}
function syncDrawer(){syncWorkspaceLayout();}
