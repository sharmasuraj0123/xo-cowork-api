/* Vertical branching-growth timeline — orchestrator. Every space gets its
   own renderer (time now runs bottom=oldest to top=newest/"now" in all
   three, matching the growth metaphor):
     Projects     -> timeline_commit_graph.js   (one-to-one with git commits)
     Sessions     -> timeline_braided_streams.js (agent edit-burst diffs)
     Environments -> timeline_split_trunk.js    (commits rolled up by
                                                  business-purpose cluster;
                                                  split ins/del silhouette)
   This module owns data fetching, window/group filtering, the shared hover
   popover, and dispatch; each renderer is a pure function of (svg, W, H,
   grouped, opts) — see RENDER CONTRACT below. Independent of atlas.js's
   boot(): own lazy fetch on first activation, own state. atlas.js's
   hooks.setActiveView delegates here for every space. */
import {apiFetch} from '../core/api.js';
import {renderSplitTrunk} from './timeline_split_trunk.js';
import {renderCommitGraph} from './timeline_commit_graph.js';
import {renderBraidedStreams} from './timeline_braided_streams.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const SVGNS='http://www.w3.org/2000/svg';
const tok=n=>{n=Number(n)||0;const s=n<0?'-':'';n=Math.abs(n);return s+(n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n));};
export const dtfmt=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';
export const hexA=(h,a)=>{h=h.replace('#','');const r=parseInt(h.slice(0,2),16),g=parseInt(h.slice(2,4),16),b=parseInt(h.slice(4,6),16);return `rgba(${r},${g},${b},${a})`;};

/* Per-space config: which dataset, how to derive groups from its events,
   and copy for the header. "kind" selects the renderer. */
const SPACE_CONFIG={
  output:{
    kind:'commit-graph', url:'data/commits.json',
    title:'The workspace, one commit at a time.',
    sub:'Every commit across every project and its worktrees, oldest at the bottom growing to now at the top.',
    groupsFrom:d=>(d.projects||[]).map(p=>({id:p.id,label:p.label,count:p.commits})),
    groupField:'project', maxGroups:8,
  },
  sessions:{
    kind:'braided-streams', url:'data/session_diffs.json',
    title:'Sessions, braided over time.',
    sub:'Agent edit activity per project — worktree sessions braid alongside the main stream and rejoin when they merge back.',
    groupsFrom:d=>(d.projects||[]).map(p=>({id:p.id,label:p.label,count:p.commits})),
    groupField:'project', maxGroups:8,
  },
  environments:{
    kind:'split-trunk', url:'data/environment_commits.json',
    title:'The workspace, growing by purpose.',
    sub:'Each cluster’s trunk splits at its spine — every line ever added grows the green side, every line ever removed grows the red side, both on one √-scale lines ruler.',
    groupsFrom:d=>(d.clusters||[]).map(c=>({id:c.id,label:c.label,count:c.projects})),
    groupField:'category', maxGroups:5,
  },
};
const CLUSTER_COLOR={app:'#6fb7e0',ops:'#e8a15c',wiki:'#7fd0a8',docs:'#c792ea',customer:'#e0708a'};
const PALETTE=['#a8d94f','#6fb7e0','#e8a15c','#c792ea','#e0708a','#7fd0a8','#d6c86a','#9b8fd6'];
function colorFor(id,index){return CLUSTER_COLOR[id]||PALETTE[index%PALETTE.length];}

const WINS=[['today','Today'],['7d','7 days'],['30d','30 days'],['all','All']];
const WDAYS={today:1,'7d':7,'30d':30,all:null};

const cache={};             // url -> payload (or {error})
let mode=null,cfg=null,win='all',selectedGroups=null; // selectedGroups: Set of ids or null=default top-N
let popEl=null,hideT=null;

function cutoffMs(){const d=WDAYS[win];return d==null?null:Date.now()-d*864e5;}

async function loadData(url){
  if(cache[url])return cache[url];
  const res=await apiFetch(url);
  cache[url]=res.ok?res.data:{error:res.error||'unavailable'};
  return cache[url];
}

/* ---- shared hover popover (all three renderers call opts.onHover/onLeave) ---- */
function popoverHtml(e){
  const files=e.files&&e.files.length?`<div class="cp-files">${e.files.map(esc).join(', ')}${e.files_count>e.files.length?` +${e.files_count-e.files.length} more`:''}</div>`:'';
  const meta=[e.project_label||e.project,e.worktree?`worktree · ${e.worktree}`:null,e.sha,e.author,dtfmt(e.date)].filter(Boolean).join(' · ');
  return `<div class="cp-title">${esc(e.title)}</div>
    <div class="cp-meta">${esc(meta)}</div>
    <div class="cp-diff"><span class="p">+${e.insertions}</span><span class="m">-${e.deletions}</span></div>
    ${files}`;
}
function showPopover(e,clientX,clientY){showPopoverIn(document.getElementById('tplot'),e,clientX,clientY);}
function movePopover(clientX,clientY){movePopoverIn(document.getElementById('tplot'),clientX,clientY);}
/* Container-relative variants: the panel's mini preview (a small chart
   inside #panel-scroll, not #tplot) reuses the exact same rich popover by
   passing its own container rather than hardcoding the main Timeline's. */
export function showPopoverIn(container,e,clientX,clientY){
  clearTimeout(hideT);
  const p=ensurePopoverIn(container);
  const rect=container.getBoundingClientRect();
  p.innerHTML=popoverHtml(e);
  let x=clientX-rect.left+14,y=clientY-rect.top-10;
  if(x+340>rect.width)x=clientX-rect.left-354;
  p.style.left=Math.max(4,x)+'px';
  p.style.top=Math.max(4,y)+'px';
  p.classList.add('is-on');
}
export function movePopoverIn(container,clientX,clientY){
  if(!popEl||!popEl.classList.contains('is-on'))return;
  const r=container.getBoundingClientRect();
  let x=clientX-r.left+14;
  if(x+340>r.width)x=clientX-r.left-354;
  popEl.style.left=Math.max(4,x)+'px';
  popEl.style.top=Math.max(4,clientY-r.top-10)+'px';
}
export function hidePopoverSoon(){clearTimeout(hideT);hideT=setTimeout(()=>popEl&&popEl.classList.remove('is-on'),120);}
function ensurePopoverIn(container){
  if(popEl&&popEl.parentElement===container)return popEl;
  popEl=document.createElement('div');
  popEl.className='cpop';
  container.appendChild(popEl);
  return popEl;
}

function buildWinButtons(){
  const el=document.getElementById('cwin');
  el.innerHTML=WINS.map(([k,l])=>`<button data-win="${k}" class="${k===win?'is-on':''}">${esc(l)}</button>`).join('');
  el.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    if(b.dataset.win===win)return;
    win=b.dataset.win;
    [...el.children].forEach(x=>x.classList.toggle('is-on',x===b));
    renderPlot();
  }));
}
function buildChips(allGroups){
  const el=document.getElementById('cchips');
  if(selectedGroups===null){
    selectedGroups=new Set(allGroups.slice(0,cfg.maxGroups).map(g=>g.id));
  }
  el.innerHTML=allGroups.map(g=>`<button data-g="${esc(g.id)}" class="${selectedGroups.has(g.id)?'is-on':''}">${esc(g.label)}<span class="n">${g.count}</span></button>`).join('');
  el.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    const id=b.dataset.g;
    if(selectedGroups.has(id))selectedGroups.delete(id);else selectedGroups.add(id);
    b.classList.toggle('is-on');
    renderPlot();
  }));
}

function groupEvents(data){
  const cutoff=cutoffMs();
  const dated=(data.events||[]).filter(e=>e.date&&(cutoff==null||+new Date(e.date)>=cutoff));
  const byGroup=new Map();
  const kept=[];  // dated AND group-filtered — what's actually rendered, for stats to match
  for(const e of dated){
    const gid=e[cfg.groupField]||'(unknown)';
    if(selectedGroups&&selectedGroups.size&&!selectedGroups.has(gid))continue;
    if(!byGroup.has(gid))byGroup.set(gid,[]);
    byGroup.get(gid).push(e);
    kept.push(e);
  }
  const allGroups=cfg.groupsFrom(data);
  const labelOf=new Map(allGroups.map(g=>[g.id,g.label]));
  const groups=[...byGroup.entries()].map(([id,evs],i)=>({
    id,label:labelOf.get(id)||id,color:colorFor(id,i),
    events:evs.slice().sort((a,b)=>+new Date(a.date)-+new Date(b.date)),
  }));
  // stable order: same order as the chip list (activity-ranked), not insertion order
  const order=new Map(allGroups.map((g,i)=>[g.id,i]));
  groups.sort((a,b)=>(order.get(a.id)??999)-(order.get(b.id)??999));
  return {groups,events:kept};
}

function updateStats(events,groupCount){
  const el=document.getElementById('cstats');
  if(!el)return;
  const ins=events.reduce((a,e)=>a+e.insertions,0),del=events.reduce((a,e)=>a+e.deletions,0);
  const noun=mode==='sessions'?'edit burst':'commit';
  el.innerHTML=`<b>${events.length}</b> ${esc(noun)}${events.length===1?'':'s'} across <b>${groupCount}</b> ${mode==='environments'?'cluster':'project'}${groupCount===1?'':'s'} · <span style="color:var(--ok)">+${tok(ins)}</span> <span style="color:var(--err)">-${tok(del)}</span>`;
}

function renderPlot(){
  const tplot=document.getElementById('tplot');
  const data=cache[cfg.url];
  popEl=null;
  tplot.innerHTML='';
  if(!data||data.error){
    tplot.innerHTML=`<div class="cempty">Timeline data unavailable${data&&data.error?' · '+esc(data.error):''}</div>`;
    return;
  }
  const {groups,events}=groupEvents(data);
  updateStats(events,groups.length);
  if(!groups.length||!events.length){
    tplot.innerHTML='<div class="cempty">Nothing in this window/selection — try a wider window or pick a different project/cluster.</div>';
    return;
  }
  const W=tplot.clientWidth,H=tplot.clientHeight;
  if(W<80||H<80)return;
  const svg=document.createElementNS(SVGNS,'svg');
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  tplot.appendChild(svg);

  const cutoff=cutoffMs();
  const allDates=events.map(e=>+new Date(e.date));
  const t0=cutoff!=null?cutoff:Math.min(...allDates);
  const t1=Math.max(Date.now(),...allDates);

  const opts={
    onHover:(e,x,y)=>showPopover(e,x,y),
    onMove:(x,y)=>movePopover(x,y),
    onLeave:()=>hidePopoverSoon(),
    hexA, esc,
    colorIns:'#7fd08a', colorDel:'#c8674c', colorAccent:'#a8d94f',
    colorInk:'#e9e4d9', colorInk3:'#7d786d', colorLine:'rgba(233,228,217,.10)',
    fmtDate:dtfmt,
  };
  const grouped={groups,t0,t1};
  try{
    if(cfg.kind==='split-trunk')renderSplitTrunk(svg,W,H,grouped,opts);
    else if(cfg.kind==='commit-graph')renderCommitGraph(svg,W,H,grouped,opts);
    else if(cfg.kind==='braided-streams')renderBraidedStreams(svg,W,H,grouped,opts);
  }catch(err){
    console.error('commit_timeline: renderer failed ('+cfg.kind+'):',err);
    tplot.innerHTML=`<div class="cempty">This view hit a rendering error — see console. Try a narrower window or fewer projects.</div>`;
  }
}

/* A group id requested via selectTimelineGroup before (or during) a mount —
   applied atomically by mountCommitTimeline itself rather than left to a
   caller's timing. atlas.js's registry drives tab switches through an
   ASYNC switchTo() that conditionally awaits a first-visit mount step, so
   "call go('time') then call selectTimelineGroup right after" is not a
   reliable ordering: depending on whether Timeline was already mounted,
   mountCommitTimeline can end up invoked either before or after that next
   line runs. A pending flag mountCommitTimeline consumes as its own first
   move sidesteps the race entirely — see traceOnTimeline in atlas.js,
   which now calls selectTimelineGroup BEFORE go('time'). */
let pendingSelect=null;

export async function mountCommitTimeline(nextMode){
  mode=nextMode;
  cfg=SPACE_CONFIG[mode]||SPACE_CONFIG.output;
  selectedGroups=pendingSelect?new Set([pendingSelect]):null;  // fresh default top-N, unless a selection is pending
  pendingSelect=null;
  document.getElementById('cthead').hidden=false;
  document.getElementById('cchips').hidden=false;
  const h2=document.querySelector('#view-time .thead h2'),sub=document.getElementById('tsub');
  if(h2)h2.textContent=cfg.title;
  if(sub)sub.textContent=cfg.sub;
  const tplot=document.getElementById('tplot');
  tplot.innerHTML='<div class="cempty">Loading timeline…</div>';

  const data=await loadData(cfg.url);
  if(mode!==nextMode)return;  // a second space switch landed before this resolved
  if(data.error){
    tplot.innerHTML=`<div class="cempty">Timeline data unavailable · ${esc(data.error)}<br><button class="ghost" id="cretry" style="margin-top:10px">Retry</button></div>`;
    document.getElementById('cretry')?.addEventListener('click',()=>{delete cache[cfg.url];mountCommitTimeline(mode);});
    return;
  }
  buildWinButtons();
  buildChips(cfg.groupsFrom(data));
  renderPlot();
}

export function resizeCommitTimeline(){
  if(cfg&&!cache[cfg.url]?.error)renderPlot();
}

/* Called from the graph panel's "Show on timeline" action: narrow the
   timeline to one project/cluster, matching the id space the CURRENT
   space's groupField uses (a Projects-space project id, a Sessions-space
   project id, or an Environments-space cluster id — atlas.js resolves
   which before calling this, since only it knows the clicked node's own
   space). Call this BEFORE go('time') if Timeline may not be mounted yet
   (see the pendingSelect comment above) — it always also applies
   immediately when a mounted, cached timeline is already on screen, so
   re-filtering while already on the tab works too. A no-op id-less call is
   ignored; an unresolvable id just shows as "0 events" once mounted (e.g.
   the graph and timeline datasets briefly disagree right after a space
   switch) rather than silently doing nothing. */
export function selectTimelineGroup(id){
  if(!id)return;
  pendingSelect=id;
  if(!cfg||!cache[cfg.url]||cache[cfg.url].error)return;  // not mounted+ready yet: mountCommitTimeline will pick this up itself
  selectedGroups=new Set([id]);
  document.querySelectorAll('#cchips button').forEach(b=>b.classList.toggle('is-on',b.dataset.g===id));
  renderPlot();
}

/* Single-group history for the graph panel's inline mini preview — same
   datasets and cache as the full Timeline (opening the panel before ever
   visiting Timeline still only fetches once), filtered down to one node's
   worth of events instead of the multi-group view. Returns {group,t0,t1}
   ready to hand a renderer, or {empty:true}/{error} if there's nothing to
   show. Never throws. */
export async function getGroupHistory(url,field,value,label){
  const data=await loadData(url);
  if(data.error)return {error:data.error};
  const events=(data.events||[])
    .filter(e=>e[field]===value&&e.date)
    .sort((a,b)=>+new Date(a.date)-+new Date(b.date));
  if(!events.length)return {empty:true};
  const dates=events.map(e=>+new Date(e.date));
  const t0=Math.min(...dates),t1=Math.max(Date.now(),...dates);
  return {group:{id:value,label:label||events[0].project_label||value,color:'#a8d94f',events},t0,t1};
}
