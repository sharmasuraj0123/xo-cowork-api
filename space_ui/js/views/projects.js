/* Projects tab — xo-projects observability (design direction 2026-07-15).
   Read-only v1: the project list (GET /api/xo-projects) with a per-project
   drawer showing the live todo board (.xo/todos.json via the watcher), open
   sessions, and the recent timeline. Every drawer panel is its own fetch —
   one dead source degrades one panel. Writes (todos CRUD, backup/restore)
   are deliberately not wired yet; the sync-vs-git decision is open. */
import {API_BASE,apiFetch} from '../core/api.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dtfmt=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'—';
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
function panelFail(res){
  if(res.notImplemented)return'<div class="prj-note">not available for the active agent</div>';
  if(res.offline)return'<div class="prj-note">xo-cowork-api is unreachable</div>';
  return'<div class="prj-note">'+esc(res.error)+'</div>';
}

/* status display order + chip class per todo status */
const ST_ORDER={in_progress:0,pending:1,blocked:2,completed:3,cancelled:4};
const stChip=st=>'<span class="tchip st-'+esc(st)+'">'+esc(st.replace('_',' '))+'</span>';

function rTodos(d){
  const rows=[];
  for(const [sid,sess] of Object.entries(d.sessions||{})){
    for(const t of sess.todos||[])rows.push({t,runtime:sess.runtime||'',sid});
  }
  if(!rows.length)return'<div class="prj-note">no todos recorded yet</div>';
  rows.sort((a,b)=>(ST_ORDER[a.t.status]??9)-(ST_ORDER[b.t.status]??9));
  const shown=rows.slice(0,30);
  return'<div class="prj-todos">'
    +shown.map(({t,runtime})=>'<div class="prj-todo">'+stChip(t.status)
      +'<span class="tcontent'+(t.status==='completed'||t.status==='cancelled'?' done':'')+'">'+esc(t.content)+'</span>'
      +(runtime?'<span class="truntime">'+esc(runtime)+'</span>':'')+'</div>').join('')
    +(rows.length>shown.length?'<div class="prj-note">+'+(rows.length-shown.length)+' more</div>':'')
    +'</div>';
}
function rActivity(d){
  const ss=d.open_sessions||[];
  if(!ss.length)return'<div class="prj-note">no open sessions</div>';
  return'<div class="prj-list">'+ss.map(s=>'<div class="prj-li">'
    +'<b>'+esc(s.agent)+'</b>'+(s.runtime?' <span class="truntime">'+esc(s.runtime)+'</span>':'')
    +'<span class="tmuted">opened '+rel(s.opened_at)+' · active '+rel(s.last_activity_at)+'</span>'
    +'</div>').join('')+'</div>';
}
function rTimeline(d){
  const evs=d.events||[];
  if(!evs.length)return'<div class="prj-note">no events yet (the watcher hasn’t emitted any for this project)</div>';
  return'<div class="prj-list">'+evs.slice(0,20).map(e=>'<div class="prj-li">'
    +'<span class="tchip">'+esc(e.type)+'</span>'
    +(e.runtime?'<span class="truntime">'+esc(e.runtime)+'</span>':'')
    +'<span class="tmuted">'+rel(e.ts)+'</span>'
    +'</div>').join('')+'</div>';
}

const PANELS=[
  {key:'todos',   title:'Todos',        path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/todos',            render:rTodos},
  {key:'activity',title:'Open sessions',path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/activity',         render:rActivity},
  {key:'timeline',title:'Recent events',path:id=>'/api/xo-projects/'+encodeURIComponent(id)+'/timeline?limit=20',render:rTimeline},
];

let root=null,items=null,expanded=null;

export default {
  id:'projects',label:'Projects',order:5,
  async mount(el){
    root=el;
    el.innerHTML='<div class="prj"><div class="prj-note">loading projects…</div></div>';
    await loadList();
  },
  show(){/* keep whatever the user had open; Refresh re-fetches */}
};

async function loadList(){
  const res=await apiFetch(API_BASE+'/api/xo-projects');
  const box=root.querySelector('.prj');
  if(!res.ok){box.innerHTML='<div class="prj-head"><span class="prj-eyebrow">PROJECTS</span></div>'+panelFail(res);return;}
  items=res.data.items||[];
  expanded=null;
  render();
}
function render(){
  const un=items.filter(p=>p.unscaffolded).length;
  root.querySelector('.prj').innerHTML=
    '<div class="prj-head"><span class="prj-eyebrow">PROJECTS · '+items.length
      +(un?' · '+un+' unscaffolded':'')+'</span>'
    +'<span class="prj-spacer"></span>'
    +'<button class="sess-refresh" id="prj-refresh" title="Re-fetch the project list">&#8635; Refresh</button></div>'
    +(items.length?'<div class="prj-rows">'+items.map(rowHTML).join('')+'</div>'
      :'<div class="prj-note">no projects in the workspace yet</div>');
  document.getElementById('prj-refresh').addEventListener('click',loadList);
  root.querySelectorAll('.prj-row-head').forEach(h=>h.addEventListener('click',()=>toggle(h.dataset.id)));
  if(expanded)fillDrawer(expanded);
}
function rowHTML(p){
  const open=expanded===p.id;
  return'<div class="prj-row'+(open?' is-open':'')+'" id="prj-row-'+esc(p.id)+'">'
    +'<div class="prj-row-head" data-id="'+esc(p.id)+'">'
    +'<span class="caret">'+(open?'&#9662;':'&#9656;')+'</span>'
    +'<b>'+esc(p.display_name)+'</b>'
    +(p.id!==p.display_name?'<span class="truntime">'+esc(p.id)+'</span>':'')
    +(p.unscaffolded?'<span class="tchip st-blocked">unscaffolded</span>':'')
    +'<span class="prj-spacer"></span>'
    +'<span class="tmuted">'+(p.created_at?'created '+rel(p.created_at):'')+'</span>'
    +'</div>'
    +(open?'<div class="prj-drawer">'
      +(p.description?'<p class="prj-desc">'+esc(p.description)+'</p>':'')
      +'<div class="prj-panels">'+PANELS.map(pn=>
        '<div class="prj-panel"><div class="prj-ptitle">'+pn.title+'</div>'
        +'<div class="prj-pbody" id="prjp-'+pn.key+'"><div class="prj-note">loading…</div></div></div>').join('')
      +'</div></div>':'')
    +'</div>';
}
function toggle(id){
  expanded=expanded===id?null:id;
  render();
}
/* three independent fetches per drawer — no barrier, no shared failure */
function fillDrawer(id){
  for(const pn of PANELS)fillPanel(id,pn);
}
async function fillPanel(id,pn){
  const res=await apiFetch(API_BASE+pn.path(id));
  if(expanded!==id)return; /* drawer changed while in flight */
  const el=document.getElementById('prjp-'+pn.key);
  if(!el)return;
  el.innerHTML=res.ok?pn.render(res.data):panelFail(res);
}
