/* Projects tab — xo-project sharing + sync (design 2026-07-16).
   Always-open cards (no drawers): each project shows a sync-state chip, a
   minimal commit feed from local git (the relay's origin/<watch-branch> view,
   with the "N new, not yet applied" count), and the share panel (share by
   workspace id, member list, revoke). A strip above the cards shows the relay
   poller's health. Every section is its own fetch — one dead endpoint degrades
   one section (bulkheads); renders replace, never append (idempotency); the
   status strip refreshes on a slotted interval; Share/Revoke disable while
   pending (writes are never single-flighted). */
import {API_BASE,apiFetch} from '../core/api.js';
import {setSlottedInterval} from '../core/store.js';
import {toast} from '../core/ui.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function rel(iso){
  if(!iso)return'—';
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(s))return'—';
  if(s<60)return'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  if(s<86400*30)return Math.floor(s/86400)+'d ago';
  return new Date(iso).toLocaleDateString(undefined,{dateStyle:'medium'});
}
function secFail(res){
  if(res.notImplemented)return'<div class="prj-note">not available for the active agent</div>';
  if(res.offline)return'<div class="prj-note">xo-cowork-api is unreachable</div>';
  return'<div class="prj-note">'+esc(res.error)+'</div>';
}

let root=null,items=null,relay=null;

export default {
  id:'projects',label:'Projects',order:5,
  async mount(el){
    root=el;
    el.innerHTML='<div class="prj"><div class="prj-note">loading projects…</div></div>';
    await loadAll();
    /* slotted: remounting or re-calling can never stack a second interval */
    setSlottedInterval('projects-relay',refreshStatus,60000);
  },
  show(){refreshStatus();}
};

async function loadAll(){
  const res=await apiFetch(API_BASE+'/api/xo-projects');
  const box=root.querySelector('.prj');
  if(!res.ok){box.innerHTML='<div class="prj-head"><span class="prj-eyebrow">PROJECTS</span></div>'+secFail(res);return;}
  items=res.data.items||[];
  const st=await apiFetch(API_BASE+'/api/relay/status');
  relay=st.ok?st.data:null;
  render();
  for(const p of items){
    if(p.unscaffolded)continue;
    fillCommits(p.id);
    if(memberState(p)==='live')fillMembers(p.id);   /* otherwise no /members call at all */
  }
}

async function refreshStatus(){
  const st=await apiFetch(API_BASE+'/api/relay/status');
  if(!st.ok||!root||!items)return;
  relay=st.data;
  const strip=root.querySelector('#prj-strip');
  if(strip)strip.innerHTML=stripHTML();
  for(const p of items){
    const chip=document.getElementById('prj-chip-'+p.id);
    if(chip)chip.innerHTML=chipHTML(p);
    if(p.unscaffolded)continue;
    /* a poll that flips a project to shared is what unlocks the member list */
    const el=membersBox(p.id),st=memberState(p);
    if(!el||el.dataset.members===st)continue;
    el.dataset.members=st;
    if(st==='live')fillMembers(p.id);else paintIdle(el,st);
  }
}

function relayEntry(projectId){
  if(!relay||!relay.repos)return null;
  for(const r of Object.values(relay.repos))if(r.project===projectId)return r;
  return null;
}

/* Relay state is the single source of truth for "is this shared", because the
   chip beside the panel reads it too and the two must never disagree. /members
   reads the swarm, which knows about an A->B share instantly; the relay only
   learns on its next poll (dormant 10m / active 50s), so fetching earlier would
   print member rows next to a "solo" chip.

   A missing relay entry is NOT proof of "not shared" — status.py is in-memory
   and restarts empty, so every project looks unshared until the first tick.
   Answer and silence get separate states; only `solo` suppresses the rows as a
   statement of fact, and only `live` is allowed to fetch. */
function memberState(p){
  if(!relay)return'unknown';
  if(relay.cadence==='parked')return'disabled';
  if(!relay.last_poll_at||relay.last_poll_ok===false)return'unknown';
  const e=relayEntry(p.id);
  return e&&e.shared?'live':'solo';
}
const IDLE_NOTE={
  disabled:'sharing is disabled for this workspace',
  unknown:'waiting for the relay to report',
  solo:'not shared yet',
};
/* the card element carries its own state — no module-level mirror to drift */
function membersBox(id){return document.getElementById('prj-members-'+id);}
function paintIdle(el,st){el.dataset.members=st;el.innerHTML='<div class="prj-note">'+IDLE_NOTE[st]+'</div>';}

function stripHTML(){
  if(!relay)return'<span class="prj-note">relay status unavailable</span>';
  if(relay.cadence==='parked'){
    return relay.workspace_configured===false
      ?'<span class="tchip st-blocked">relay parked</span><span class="tmuted">no workspace id configured — sharing is disabled</span>'
      :'<span class="tchip st-blocked">relay off</span><span class="tmuted">RELAY_ENABLED=false</span>';
  }
  const ok=relay.last_poll_ok;
  return'<span class="tchip'+(ok===false?' st-blocked':'')+'">'+esc(relay.cadence)+'</span>'
    +'<span class="tmuted">last poll '+rel(relay.last_poll_at)+(ok===false?' · failed':'')+'</span>'
    +'<span class="tmuted">watching '+esc(relay.watch_branch||'main')+'</span>';
}

function chipHTML(p){
  if(p.unscaffolded)return'<span class="tchip st-blocked">unscaffolded</span>';
  const e=relayEntry(p.id);
  if(!e)return'<span class="tchip">solo</span>';
  if(e.pending_github)return'<span class="tchip st-blocked">connect GitHub to sync</span>';
  if(e.last_error)return'<span class="tchip st-blocked">sync error</span>';
  if(e.shared)return'<span class="tchip st-shared">shared</span>'
    +(e.last_fetch_at?'<span class="tmuted">synced '+rel(e.last_fetch_at)+'</span>':'');
  return'<span class="tchip">solo</span>';
}

function render(){
  root.querySelector('.prj').innerHTML=
    '<div class="prj-head"><span class="prj-eyebrow">PROJECTS · '+items.length+'</span>'
    +'<span class="prj-spacer"></span>'
    +'<button class="sess-refresh" id="prj-refresh" title="Re-fetch everything">&#8635; Refresh</button></div>'
    +'<div class="prj-strip" id="prj-strip">'+stripHTML()+'</div>'
    +(items.length?'<div class="prj-cards">'+items.map(cardHTML).join('')+'</div>'
      :'<div class="prj-note">no projects in the workspace yet</div>');
  document.getElementById('prj-refresh').addEventListener('click',loadAll);
  root.querySelectorAll('.prj-share-btn').forEach(b=>b.addEventListener('click',()=>share(b.dataset.id)));
}

function cardHTML(p){
  return'<div class="prj-card" id="prj-card-'+esc(p.id)+'">'
    +'<div class="prj-card-head">'
    +'<b>'+esc(p.display_name)+'</b>'
    +(p.id!==p.display_name?'<span class="truntime">'+esc(p.id)+'</span>':'')
    +'<span id="prj-chip-'+esc(p.id)+'" class="prj-chipbox">'+chipHTML(p)+'</span>'
    +'<span class="prj-spacer"></span>'
    +'<span class="tmuted">'+(p.created_at?'created '+rel(p.created_at):'')+'</span>'
    +'</div>'
    +(p.description?'<p class="prj-desc">'+esc(p.description)+'</p>':'')
    +(p.unscaffolded?'':
      '<div class="prj-sections">'
      +'<div class="prj-sec"><div class="prj-ptitle">Commits</div>'
      +'<div id="prj-commits-'+esc(p.id)+'"><div class="prj-note">loading…</div></div></div>'
      +'<div class="prj-sec"><div class="prj-ptitle">Sharing</div>'
      +'<div id="prj-members-'+esc(p.id)+'" data-members="'+memberState(p)+'">'
      +'<div class="prj-note">'+(memberState(p)==='live'?'loading…':IDLE_NOTE[memberState(p)])+'</div></div>'
      +'<div class="prj-shareform">'
      +'<input class="prj-input" id="prj-ws-'+esc(p.id)+'" placeholder="recipient workspace id" spellcheck="false">'
      +'<button class="prj-btn prj-share-btn" data-id="'+esc(p.id)+'">Share</button>'
      +'</div></div>'
      +'</div>')
    +'</div>';
}

async function fillCommits(id){
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/commits?limit=15');
  const el=document.getElementById('prj-commits-'+id);
  if(!el)return;
  if(!res.ok){el.innerHTML=secFail(res);return;}
  const d=res.data,list=d.commits||[];
  const newN=d.behind|0;
  el.innerHTML=
    (newN>0?'<div class="prj-newline"><span class="tchip st-shared">'+newN+' new</span>'
      +'<span class="tmuted">fetched on origin/'+esc(d.branch)+', not yet applied</span></div>':'')
    +(list.length?'<div class="prj-commits">'+list.map((c,i)=>
      '<div class="prj-commit'+(i<newN?' is-new':'')+'">'
      +'<code class="prj-hash">'+esc(c.hash.slice(0,8))+'</code>'
      +'<span class="prj-subj">'+esc(c.subject)+'</span>'
      +'<span class="tmuted">'+esc(c.author)+' · '+rel(c.date)+'</span>'
      +'</div>').join('')+'</div>'
      :'<div class="prj-note">no commits visible ('+esc(d.source)+')</div>');
}

async function fillMembers(id){
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/members');
  const el=document.getElementById('prj-members-'+id);
  if(!el)return;
  if(!res.ok){
    /* 404 no_git_origin / 403 not-a-member / swarm down: sharing may still be
       possible (the first share creates the group) — the form stays below */
    el.innerHTML='<div class="prj-note">'+esc(res.error)+'</div>';
    return;
  }
  const own=res.data.own_workspace_id,ms=res.data.members||[];
  const iOwn=ms.some(m=>m.role==='owner'&&m.workspace_id===own);
  el.innerHTML=ms.length?'<div class="prj-members">'+ms.map(m=>
    '<div class="prj-member'+(m.status==='revoked'?' is-revoked':'')+'">'
    +'<code>'+esc(m.workspace_id)+'</code>'
    +'<span class="tchip">'+esc(m.role)+'</span>'
    +(m.status==='revoked'?'<span class="tchip st-blocked">revoked</span>':'')
    +(m.bound===false&&m.status==='active'?'<span class="tmuted" title="that workspace has not polled yet">not seen yet</span>':'')
    +(m.workspace_id===own?'<span class="tmuted">(this workspace)</span>':'')
    +'<span class="prj-spacer"></span>'
    +(iOwn&&m.role!=='owner'&&m.status==='active'
      ?'<button class="prj-btn prj-revoke-btn" data-id="'+esc(id)+'" data-ws="'+esc(m.workspace_id)+'">Revoke</button>':'')
    +'</div>').join('')+'</div>'
    :'<div class="prj-note">not shared with anyone yet</div>';
  el.querySelectorAll('.prj-revoke-btn').forEach(b=>b.addEventListener('click',()=>revoke(b.dataset.id,b.dataset.ws)));
}

async function share(id){
  const input=document.getElementById('prj-ws-'+id);
  const btn=root.querySelector('.prj-share-btn[data-id="'+id+'"]');
  const ws=(input&&input.value||'').trim();
  if(!ws){toast('enter the recipient’s workspace id');return;}
  if(btn)btn.disabled=true;               /* never single-flight a write */
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/share',
    {method:'POST',body:{workspace_id:ws}});
  if(btn)btn.disabled=false;
  if(!res.ok){toast('share failed: '+res.error);return;}
  toast('shared with '+ws);
  if(input)input.value='';
  /* the swarm has it, our relay does not yet. Report the pending write rather
     than fetching rows the chip would contradict — the next poll promotes it. */
  const el=membersBox(id);
  if(el&&el.dataset.members!=='live')
    el.innerHTML='<div class="prj-note">shared with '+esc(ws)+' · appears here after the next poll</div>';
  else fillMembers(id);
}

async function revoke(id,ws){
  const btn=root.querySelector('.prj-revoke-btn[data-id="'+id+'"][data-ws="'+ws+'"]');
  if(btn)btn.disabled=true;
  const res=await apiFetch(API_BASE+'/api/xo-projects/'+encodeURIComponent(id)+'/revoke',
    {method:'POST',body:{workspace_id:ws}});
  if(btn)btn.disabled=false;
  if(!res.ok){toast('revoke failed: '+res.error);return;}
  toast('revoked '+ws);
  fillMembers(id);
}
