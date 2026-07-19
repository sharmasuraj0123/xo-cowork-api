/* Environments dashboard — the Dashboard page when the sidebar's
   Environments space is selected. Shows this workspace as an environment:
   work items (watcher todos, else recent per-project activity), teammates
   (open sessions and the runtimes seen this week), and environment status.
   Data: GET data/overview.json (the .xo state the watcher maintains). */
import {apiFetch} from '../core/api.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const rel=iso=>{
  if(!iso)return '—';
  const s=(Date.now()-+new Date(iso))/1000;
  if(s<90)return 'just now';
  if(s<3600)return Math.round(s/60)+'m ago';
  if(s<86400)return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
};

export default {
  id:'environments',label:'Environments',order:0,
  async mount(el){
    const wrap=document.createElement('div');
    wrap.className='ovwrap';
    el.appendChild(wrap);
    let D=null,failed=null;

    async function load(){
      wrap.innerHTML='<div class="ovload">reading environment state…</div>';
      const res=await apiFetch('data/overview.json');
      if(!res.ok){failed=res.error||'unavailable';D=null;render();return;}
      D=res.data;failed=null;render();
    }

    function workItems(){
      const todos=Array.isArray(D.todos)?D.todos:(D.todos&&D.todos.items)||null;
      if(todos&&todos.length)return todos.slice(0,12).map(t=>`<div class="xrow">
        <span class="xdot ${t.done||t.status==='done'?'':'live'}"></span>
        <span class="xr-main">${esc(t.title||t.text||t.content||String(t))}</span>
        <span class="xr-meta">${esc(t.project_id||t.project||'')}</span>
        <span class="xr-time">${esc(t.status||'')}</span>
      </div>`).join('');
      /* no todos collected yet: recent activity per project stands in */
      const seen=new Map();
      for(const e of D.timeline||[]){
        if(!e.project_id||seen.has(e.project_id))continue;
        seen.set(e.project_id,e);
        if(seen.size>=8)break;
      }
      const rows=[...seen.values()].map(e=>`<div class="xrow">
        <span class="xr-main">${esc(e.project_id)}</span>
        <span class="xr-meta">${esc(e.type||'')}${e.path?' · '+esc(e.path):''}</span>
        <span class="xr-time">${rel(e.ts)}</span>
      </div>`).join('');
      return rows?`<div class="xfoot">No work items collected yet — recent activity per project:</div>${rows}`
        :'<div class="xempty">No work items collected yet.</div>';
    }

    function render(){
      if(failed){
        wrap.innerHTML=`<div class="ovload">Environment state is not readable · ${esc(failed)}
          <button class="ovbtn" id="env-retry">Retry</button></div>`;
        wrap.querySelector('#env-retry').addEventListener('click',load);
        return;
      }
      const ws=D.workspace||{},act=D.activity||{},st=D.stats||{};
      const open=act.open_sessions||[];
      const runtimes=Object.keys(st.by_runtime||{});
      const teammates=open.map(s=>`<div class="xrow">
        <span class="xdot live"></span>
        <span class="xr-main">${esc(s.agent||s.runtime||'agent')}</span>
        <span class="xr-meta">${esc(s.runtime||'')} · ${esc(s.project_id||'')} · ${esc(s.user_id||'')}</span>
        <span class="xr-time">${rel(s.last_activity_at)}</span>
      </div>`).join('')
        +runtimes.filter(r=>!open.some(s=>s.runtime===r)).map(r=>`<div class="xrow">
        <span class="xdot"></span>
        <span class="xr-main">${esc(r)}</span>
        <span class="xr-meta">runtime · idle</span><span class="xr-time"></span>
      </div>`).join('');

      wrap.innerHTML=`
        <div class="ovhead">
          <div>
            <div class="oveye">environments</div>
            <h2>${esc((D.root||'').split('/').pop()||'workspace')}</h2>
            <div class="ovsub">local environment · ${(ws.projects||[]).length} projects ·
              ${open.length} live session${open.length===1?'':'s'} · updated ${rel(ws.updated_at)}</div>
          </div>
          <div class="grow"></div>
          <button class="ovbtn" id="env-refresh">&#8635; Refresh</button>
        </div>
        <div class="ovgrid">
          <div class="ovcard wide">
            <h4>Work items</h4>
            ${workItems()}
          </div>
          <div class="ovcard wide">
            <h4>Teammates</h4>
            ${teammates||'<div class="xempty">No agents active.</div>'}
          </div>
          <div class="ovcard">
            <h4>This environment</h4>
            <div class="xkv"><span>Host</span><b>local · this machine</b></div>
            <div class="xkv"><span>Root</span><b class="mono">${esc(D.root||'—')}</b></div>
            <div class="xkv"><span>Status</span><span class="xpill is-on">online</span></div>
          </div>
        </div>`;
      wrap.querySelector('#env-refresh').addEventListener('click',load);
    }

    await load();
  },
  show(){},hide(){}
};
