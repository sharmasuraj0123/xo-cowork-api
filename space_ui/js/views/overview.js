/* Overview tab — visualizes the workspace's .xo/ state (data:
   GET data/overview.json): the xo.json manifest (runtime, models,
   connectors, channels), workspace + project census, live open sessions,
   rolling stats, and the recent event timeline. Read-only view over what
   the watcher service maintains; independent of the atlas boot. */
import {apiFetch} from '../core/api.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tok=n=>{n=Number(n)||0;return n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(Math.round(n));};
const rel=iso=>{
  if(!iso)return '—';
  const s=(Date.now()-+new Date(iso))/1000;
  if(s<90)return 'just now';
  if(s<3600)return Math.round(s/60)+'m ago';
  if(s<86400)return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
};
const pill=(label,on)=>`<span class="xpill ${on?'is-on':'is-off'}">${esc(label)}</span>`;
const kb=n=>{n=Number(n)||0;return n>=1e6?(n/1e6).toFixed(1)+' MB':n>=1e3?(n/1e3).toFixed(1)+' KB':n+' B';};
function treeHtml(node,depth){
  if(node.type==='file')
    return `<div class="tf"><span class="tfn">${esc(node.name)}</span><span class="tfs">${node.size!=null?kb(node.size):''}</span></div>`;
  const kids=(node.children||[]).map(c=>treeHtml(c,depth+1)).join('');
  const more=node.more?`<div class="tf tmore">… ${node.more} more item${node.more===1?'':'s'}</div>`:'';
  const count=(node.children||[]).length+(node.more||0);
  return `<details class="td"${depth<1?' open':''}>
    <summary><span class="tdn">${esc(node.name)}</span><span class="tfs">${count} item${count===1?'':'s'}</span></summary>
    <div class="tkids">${kids}${more}</div>
  </details>`;
}
const bars=(pairs,fmt)=>{
  /* values are coerced to numbers before hitting innerHTML: stats.json is
     watcher-written data, not trusted markup */
  const rows=pairs.map(([k,v])=>[k,Number(v)||0]);
  const max=Math.max(1,...rows.map(([,v])=>v));
  return rows.map(([k,v])=>`<div class="xbar">
    <span class="xbl">${esc(k)}</span>
    <span class="xbt"><span style="width:${Math.max(2,Math.round(v/max*100))}%"></span></span>
    <span class="xbv">${fmt?fmt(v):v}</span>
  </div>`).join('');
};

/* The Overview follows the topbar's Projects/Sessions space switcher (same
   persisted key atlas.js uses; switching spaces reloads the page, so reading
   it once per mount is enough). Projects space shows the workspace tree +
   .xo state; Sessions space shows every runtime's session-data stores. */
const SPACE=(()=>{try{return localStorage.getItem('space.graphDataset')==='sessions'?'sessions':'projects';}catch(_e){return 'projects';}})();

export default {
  id:'overview',label:'Overview',order:0,
  async mount(el){
    const wrap=document.createElement('div');
    wrap.className='ovwrap';
    el.appendChild(wrap);
    let D=null,failed=null,mode='data';  /* 'data' = trees · 'meta' = collected state */

    async function load(){
      wrap.innerHTML=`<div class="ovload">reading ${SPACE==='sessions'?'session data stores':'.xo state'}…</div>`;
      const res=await apiFetch(SPACE==='sessions'?'data/overview_sessions.json':'data/overview.json');
      if(!res.ok){failed=res.error||'unavailable';D=null;render();return;}
      D=res.data;failed=null;render();
    }

    function render(){
      if(failed){
        wrap.innerHTML=`<div class="ovload">${SPACE==='sessions'?'No runtime session data is readable':"The workspace's .xo state is not readable"} · ${esc(failed)}
          <button class="ovbtn" id="ov-retry">Retry</button></div>`;
        wrap.querySelector('#ov-retry').addEventListener('click',load);
        return;
      }
      if(SPACE==='sessions'){renderSessions();return;}
      const m=D.manifest||{},ws=D.workspace||{},st=D.stats||{},act=D.activity||{};
      renderProjects(m,ws,st,act);
    }

    /* ---- sessions space: runtime session-data stores ---- */
    const mts=m=>m?new Date(m*1000).toISOString():null;
    function storeFacts(meta){
      const rows=[];
      for(const [k,v] of Object.entries(meta||{})){
        const label=k.replace(/_/g,' ');
        if(v&&typeof v==='object'&&'files' in v){
          rows.push(`<div class="xrow"><span class="xr-main">${esc(label)}</span>
            <span class="xr-meta">${Number(v.files)||0}${v.capped?'+':''} files · ${kb(v.bytes)}</span>
            <span class="xr-time">${rel(mts(v.newest_mtime))}</span></div>`);
        }else if(v&&typeof v==='object'&&'path' in v){
          rows.push(`<div class="xrow"><span class="xr-main">${esc(label)}</span>
            <span class="xr-meta" title="${esc(v.path)}">${esc((v.path||'').split('/').pop())} · ${kb(v.bytes)}</span>
            <span class="xr-time">${rel(mts(v.mtime))}</span></div>`);
        }else{
          rows.push(`<div class="xrow"><span class="xr-main">${esc(label)}</span>
            <span class="xr-meta">${esc(String(v))}</span><span class="xr-time"></span></div>`);
        }
      }
      return rows.join('')||'<div class="xempty">No store metadata reported.</div>';
    }
    function renderSessions(){
      const sources=D.sources||[];
      const totFiles=sources.reduce((a,s)=>a+Object.values(s.meta||{})
        .reduce((b,v)=>b+(v&&typeof v==='object'&&'files' in v?(Number(v.files)||0):0),0),0);
      const dataBody=sources.map(s=>s.roots.map(r=>`
        <div class="ovcard wide treecard">
          <h4>${esc(s.label)} · ${esc(r.label)}</h4>
          <div class="ovsub mono">${esc(r.path)}</div>
          <div class="ovtree">${(r.tree.children||[]).map(c=>treeHtml(c,0)).join('')||'<div class="xempty">Empty.</div>'}${r.tree.more?`<div class="tf tmore">… ${r.tree.more} more items</div>`:''}</div>
        </div>`).join('')).join('')||'<div class="xempty">No session data stores found.</div>';
      const metaBody=`<div class="ovgrid">${sources.map(s=>`
        <div class="ovcard wide">
          <h4>${esc(s.label)} — collected stores</h4>
          ${storeFacts(s.meta)}
        </div>`).join('')}</div>`;
      wrap.innerHTML=`
        <div class="ovhead">
          <div>
            <div class="oveye">runtime session data</div>
            <h2>Sessions</h2>
            <div class="ovsub">${sources.length} runtime${sources.length===1?'':'s'} · ${totFiles} session files · updated ${rel(D.generated_at)}</div>
          </div>
          <div class="grow"></div>
          <div class="ovmodes">
            <button data-mode="data" class="${mode==='data'?'is-on':''}">Data</button>
            <button data-mode="meta" class="${mode==='meta'?'is-on':''}">Metadata</button>
          </div>
          <button class="ovbtn" id="ov-refresh">&#8635; Refresh</button>
        </div>
        ${mode==='data'?dataBody:metaBody}`;
      wrap.querySelector('#ov-refresh').addEventListener('click',load);
      wrap.querySelectorAll('.ovmodes button').forEach(b=>b.addEventListener('click',()=>{
        if(b.dataset.mode!==mode){mode=b.dataset.mode;render();}
      }));
    }

    function renderProjects(m,ws,st,act){
      const projects=ws.projects||[];
      const open=act.open_sessions||[];
      const w7=(st.rolling||{})['7d']||{};
      const models=m.models||{},data=m.data||{},chan=m.channels||{};
      const oauth=models.oauth||{},keys=models.api_keys||{};
      const mstatus=(models.status||{}).models||[];

      const openRows=open.map(s=>`<div class="xrow">
        <span class="xdot live"></span>
        <span class="xr-main">${esc(s.project_id||'—')}</span>
        <span class="xr-meta">${esc(s.runtime||'')} · ${esc(s.agent||'')}</span>
        <span class="xr-time">${rel(s.last_activity_at)}</span>
      </div>`).join('')||'<div class="xempty">No sessions open right now.</div>';

      const events=(D.timeline||[]).slice(0,14).map(e=>`<div class="xrow">
        <span class="xr-main">${esc(e.type||'event')}</span>
        <span class="xr-meta">${esc(e.project_id||'')}${e.path?' · '+esc(e.path):''}</span>
        <span class="xr-time">${rel(e.ts)}</span>
      </div>`).join('')||'<div class="xempty">No recent events.</div>';

      const byModel=Object.entries(w7.by_model||{})
        .map(([k,v])=>[k.replace(/^claude-/,''),(Number(v?.input)||0)+(Number(v?.output)||0)])
        .sort((a,b)=>b[1]-a[1]).slice(0,6);
      const byTool=Object.entries(w7.by_tool||{}).map(([k,v])=>[k,Number(v)||0])
        .sort((a,b)=>b[1]-a[1]).slice(0,6);

      /* Data mode: the live tree of the xo-projects root */
      const dataBody=`<div class="ovcard wide treecard">
          <h4>${esc((D.root||'').split('/').pop()||'workspace')} — project tree</h4>
          <div class="ovtree">${D.tree?((D.tree.children||[]).map(c=>treeHtml(c,0)).join('')||'<div class="xempty">Empty workspace.</div>'):'<div class="xempty">No tree in payload.</div>'}</div>
        </div>`;

      /* Metadata mode: what the watcher has collected under .xo/ */
      const xoFiles=(D.xo_files||[]).map(f=>`<div class="xrow">
          <span class="xr-main">.xo/${esc(f.name)}</span>
          <span class="xr-meta">${kb(f.size)}</span>
          <span class="xr-time">${rel(f.mtime?new Date(f.mtime*1000).toISOString():null)}</span>
        </div>`).join('')||'<div class="xempty">Nothing collected yet.</div>';

      const metaBody=`<div class="ovgrid">
          <div class="ovcard wide">
            <h4>.xo contents</h4>
            ${xoFiles}
          </div>`;

      wrap.innerHTML=`
        <div class="ovhead">
          <div>
            <div class="oveye">.xo workspace state</div>
            <h2>${esc((D.root||'').split('/').pop()||'workspace')}</h2>
            <div class="ovsub">${projects.length} projects · agent ${esc(m.agent||'—')} ·
              updated ${rel(ws.updated_at||st.updated_at)}</div>
          </div>
          <div class="grow"></div>
          <div class="ovmodes">
            <button data-mode="data" class="${mode==='data'?'is-on':''}">Data</button>
            <button data-mode="meta" class="${mode==='meta'?'is-on':''}">Metadata</button>
          </div>
          <button class="ovbtn" id="ov-refresh">&#8635; Refresh</button>
        </div>

        ${mode==='data'?dataBody:metaBody+`
          <div class="ovcard">
            <h4>Runtime &amp; models</h4>
            <div class="xkv"><span>Runtime</span><b>${esc(m.agent||'—')}</b></div>
            <div class="xkv"><span>Default model</span><b>${esc((models.status||{}).default||'—')}</b></div>
            ${mstatus.map(x=>`<div class="xkv"><span>${esc(x.id)}</span>${pill(x.status||'?',x.status==='ok')}</div>`).join('')}
            <div class="xpills">
              ${Object.entries(oauth).map(([k,v])=>pill('oauth · '+k,v&&v.enabled)).join('')}
              ${Object.entries(keys).filter(([k])=>k!=='enabled').map(([k,v])=>pill('key · '+k,v&&v.enabled)).join('')}
            </div>
          </div>

          <div class="ovcard">
            <h4>Connectors &amp; channels</h4>
            <div class="xpills">
              ${Object.entries(data).filter(([k])=>k!=='enabled').map(([k,v])=>pill(k.replace(/_/g,' '),v&&v.enabled)).join('')}
            </div>
            <div class="xpills">
              ${Object.entries(chan).filter(([k])=>!['enabled','status'].includes(k)).map(([k,v])=>pill(k,v&&v.enabled)).join('')}
              ${pill('secrets',(m.secrets||{}).enabled)}
            </div>
          </div>

          <div class="ovcard">
            <h4>Active now</h4>
            ${openRows}
            <div class="xfoot">${D.known_sessions?D.known_sessions+' sessions known to this workspace':''}</div>
          </div>

          <div class="ovcard">
            <h4>Last 7 days</h4>
            <div class="xstats">
              <div><b>${tok((w7.tokens||{}).input)}</b><span>tokens in</span></div>
              <div><b>${tok((w7.tokens||{}).output)}</b><span>tokens out</span></div>
              <div><b>${w7.sessions!=null?Number(w7.sessions)||0:'—'}</b><span>sessions</span></div>
              <div><b>${w7.files_edited!=null?Number(w7.files_edited)||0:'—'}</b><span>files edited</span></div>
              <div><b>${Number(w7.active_minutes)?Math.round(Number(w7.active_minutes)/60)+'h':'—'}</b><span>active</span></div>
            </div>
          </div>

          <div class="ovcard">
            <h4>Tokens by model (7d)</h4>
            ${bars(byModel,tok)||'<div class="xempty">No model activity.</div>'}
          </div>

          <div class="ovcard">
            <h4>Tool calls (7d)</h4>
            ${bars(byTool)||'<div class="xempty">No tool activity.</div>'}
          </div>

          <div class="ovcard wide">
            <h4>Recent events</h4>
            ${events}
          </div>
        </div>`}`;
      wrap.querySelector('#ov-refresh').addEventListener('click',load);
      wrap.querySelectorAll('.ovmodes button').forEach(b=>b.addEventListener('click',()=>{
        if(b.dataset.mode!==mode){mode=b.dataset.mode;render();}
      }));
    }

    await load();
  },
  show(){},hide(){}
};
