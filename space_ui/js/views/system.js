/* System tab — read-only status cards over the API's introspection endpoints
   (design spec §3.1 Tier 2). Every card is its own fetch + render: one dead
   endpoint greys out one card, never the tab. The 501 / empty-shape contract
   of the adapter architecture renders as a normal "not available" state, not
   an error. Endpoint scope is decided in the spec — models, channels,
   providers, connectors and usage are excluded by user decision. */
import {API_BASE,apiFetch} from '../core/api.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const kv=(k,v)=>v==null||v===''?'':'<div class="sys-kv"><dt>'+esc(k)+'</dt><dd>'+esc(v)+'</dd></div>';
const yn=b=>b?'yes':'no';
const base=p=>String(p||'').split(/[\\/]/).pop();
const dtfmt=iso=>iso?new Date(iso).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):null;

/* generic list card: arrays of strings or {name|id} objects */
const rCount=plural=>d=>{
  const items=Array.isArray(d)?d:[];
  if(!items.length)return'<div class="sys-note">no '+plural+' reported by the active agent</div>';
  const names=items.slice(0,10).map(x=>esc(typeof x==='string'?x:(x.name||x.id||JSON.stringify(x))));
  return'<div class="sys-big">'+items.length+'</div>'
    +'<div class="sys-chips">'+names.map(n=>'<span class="chip">'+n+'</span>').join('')
    +(items.length>10?'<span class="chip">+'+(items.length-10)+' more</span>':'')+'</div>';
};

function rHealth(d){
  return'<dl>'
    +kv('Status',d.status)
    +kv('Stage',d.stage)
    +kv('AI provider',d.ai_provider)
    +kv('Claude CLI',base(d.claude_cli))
    +kv('Codex CLI',base(d.codex_cli))
    +kv('Active sessions',d.active_sessions)
    +(d.auth&&typeof d.auth==='object'?kv('Auth',d.auth.token_source?String(d.auth.token_source)+(d.auth.user_id?' · '+d.auth.user_id:''):null):'')
    +kv('Checked',dtfmt(d.timestamp))
    +'</dl>';
}
function rSync(d){
  return'<dl>'
    +kv('Configured',yn(d.configured))
    +kv('GitHub token',d.token_source||'not found')
    +kv('GPG',d.gpg_available?'available':'missing — /setup will fail until installed')
    +'</dl>';
}
function rOnboarding(d){
  return d.completed
    ?'<dl>'+kv('Completed',dtfmt(d.completed_at)||'yes')+'</dl>'
    :'<div class="sys-note">first-run onboarding not completed</div>';
}
function rPlugins(d){
  const keys=d&&typeof d==='object'?Object.keys(d):[];
  if(!keys.length)return'<div class="sys-note">no plugins reported</div>';
  return'<dl>'+keys.slice(0,12).map(k=>kv(k,typeof d[k]==='object'?JSON.stringify(d[k]):d[k])).join('')+'</dl>';
}

/* card order is reading order: the rich, always-informative cards first */
const CARDS=[
  {key:'health',    title:'API health',              path:'/health',                      render:rHealth},
  {key:'sync',      title:'Backups · xo-projects-sync',path:'/api/xo-projects-sync/status',render:rSync},
  {key:'onboarding',title:'Onboarding',              path:'/api/onboarding',              render:rOnboarding},
  {key:'mcp',       title:'MCP servers',             path:'/api/mcp/status',              render:rCount('servers')},
  {key:'tools',     title:'Tools',                   path:'/api/tools',                   render:rCount('tools')},
  {key:'skills',    title:'Skills',                  path:'/api/skills',                  render:rCount('skills')},
  {key:'automations',title:'Automations',            path:'/api/automations',             render:rCount('automations')},
  {key:'plugins',   title:'Plugins',                 path:'/api/plugins/status',          render:rPlugins},
  {key:'memory',    title:'Workspace memory',        path:'/api/workspace-memory/list',   render:rCount('memory entries')},
];

export default {
  id:'system',label:'System',order:5,
  async mount(el){
    el.innerHTML='<div class="sys">'
      +'<div class="sys-head"><span class="sys-eyebrow">SYSTEM · read-only status</span>'
      +'<span class="sys-spacer"></span>'
      +'<button class="sess-refresh" id="sys-refresh" title="Re-fetch every card">&#8635; Refresh</button></div>'
      +'<div class="sys-grid">'
      +CARDS.map(c=>'<div class="syscard" id="sys-'+c.key+'">'
        +'<div class="sys-title">'+esc(c.title)+'<span class="pill" id="sysp-'+c.key+'"></span></div>'
        +'<div class="sys-body" id="sysb-'+c.key+'"><div class="sys-note">loading…</div></div>'
        +'</div>').join('')
      +'</div></div>';
    document.getElementById('sys-refresh').addEventListener('click',loadAll);
    loadAll();
  }
};

/* every card fetches independently and in parallel — no barrier, no shared
   failure. Re-running loadAll converges (renders replace) and concurrent
   clicks collapse into one request per path (apiFetch single-flights GETs). */
function loadAll(){
  for(const c of CARDS)loadCard(c);
}
async function loadCard(c){
  const pill=document.getElementById('sysp-'+c.key),body=document.getElementById('sysb-'+c.key);
  if(!pill||!body)return;
  pill.className='pill';
  body.innerHTML='<div class="sys-note">loading…</div>';
  const res=await apiFetch(API_BASE+c.path);
  if(!pill.isConnected)return; /* tab re-rendered while in flight */
  if(res.ok){
    pill.className='pill on';
    try{body.innerHTML=c.render(res.data);}
    catch(err){pill.className='pill off';body.innerHTML='<div class="sys-note">could not render: '+esc(err.message)+'</div>';}
  }else if(res.notImplemented){
    pill.className='pill dim';
    body.innerHTML='<div class="sys-note">not available for the active agent</div>';
  }else if(res.offline){
    pill.className='pill off';
    body.innerHTML='<div class="sys-note">xo-cowork-api is unreachable — the request never reached the server (the footer pill tracks it)</div>';
  }else{
    pill.className='pill off';
    body.innerHTML='<div class="sys-note">'+esc(res.error)+'</div>';
  }
}
