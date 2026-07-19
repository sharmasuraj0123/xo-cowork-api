/* Experiment tab — one-click, local-development XO project sandboxes.
   The browser sends only a project id. The BFF resolves the host path, keeps
   credentials server-side, and exposes a provider-neutral lifecycle snapshot.
   Writes disable while pending; one named poll replaces its predecessor. */
import {API_BASE,apiFetch} from '../core/api.js';
import {clearSlottedInterval,setSlottedInterval} from '../core/store.js';
import {toast} from '../core/ui.js';

const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ACTIVE=new Set(['starting','ready','stopping','cleanup_failed']);
const STAGES=[
  ['queued','Queued'],
  ['creating_session','Session'],
  ['cloning_project','Project'],
  ['cloning_cowork_api','XO API'],
  ['starting_space','Space'],
  ['connecting_agent','Connect'],
  ['booting_agent','Boot'],
  ['ready','Ready'],
];

let root=null,projects=[],experiments=[],options=null,selected='',launching=false,visible=false;
let actionError='',renderKey='',activeId='',sending=false,workbenchKey='';
let draft='',announcedReady='';
const pendingStops=new Set();

export default {
  id:'experiment',label:'Experiment',order:7,
  async mount(el){
    root=el;
    activeId=new URLSearchParams(location.search).get('experiment')||activeId;
    root.innerHTML='<div class="exp"><div id="exp-launcher" class="exp-launcher"></div>'
      +'<section id="exp-workbench" class="exp-workbench" hidden></section>'
      +'<div class="exp-listhead"><span class="exp-eyebrow">RECENT EXPERIMENTS</span>'
      +'<span id="exp-live" class="exp-live" role="status" aria-live="polite" aria-atomic="true"></span></div>'
      +'<div id="exp-list" class="exp-list"><div class="exp-note">loading experiments…</div></div></div>';
    await loadInitial();
  },
  show(){
    visible=true;
    refreshOptions();
    refreshExperiments();
    setSlottedInterval('space-experiments',()=>{if(visible)refreshExperiments();},2000);
  },
  hide(){visible=false;clearSlottedInterval('space-experiments');}
};

async function loadInitial(){
  const [projectRes,optionRes,experimentRes]=await Promise.all([
    apiFetch(API_BASE+'/api/xo-projects'),
    apiFetch(API_BASE+'/api/experiments/options'),
    apiFetch(API_BASE+'/api/experiments'),
  ]);
  projects=projectRes.ok?(projectRes.data.items||[]):[];
  options=optionRes.ok?optionRes.data:null;
  experiments=experimentRes.ok?(experimentRes.data.items||[]):[];
  if(!selected&&projects.length)selected=projects[0].id;
  if(!experiments.some(row=>row.id===activeId))activeId=experiments.find(row=>ACTIVE.has(row.status))?.id||experiments[0]?.id||'';
  renderLauncher(projectRes,optionRes);
  renderExperiments(experimentRes);
  renderWorkbench();
}

function renderLauncher(projectRes={ok:true},optionRes={ok:true}){
  const el=document.getElementById('exp-launcher');
  if(!el)return;
  if(!projects.some(project=>project.id===selected))selected=projects[0]?.id||'';
  const provider=options&&options.provider;
  const managedSandbox=provider&&provider.context==='sandbox';
  const listHead=root?.querySelector('.exp-listhead');
  const list=document.getElementById('exp-list');
  if(listHead)listHead.hidden=Boolean(managedSandbox);
  if(list)list.hidden=Boolean(managedSandbox);
  if(managedSandbox){
    const managerUrl=safeHttpUrl(provider.manager_url);
    el.innerHTML='<div class="exp-copy"><span class="exp-eyebrow">MANAGED SANDBOX</span>'
      +'<h1>You are already inside an experiment.</h1>'
      +'<p>This Space is the isolated copy for inspecting the selected project. New sandboxes and agent turns are managed by the parent Experiment workbench.</p>'
      +'<div class="exp-provider"><span class="exp-dot is-ready"></span><b>'+esc(provider.label||'Managed Experiment sandbox')+'</b></div></div>'
      +'<div class="exp-action"><b>Nested launches are intentionally disabled</b>'
      +'<p class="exp-data-note">The sandbox does not receive your host .env, the Agents API SDK, or Docker access. This protects the host and prevents a sandbox from creating more sandboxes.</p>'
      +(managerUrl?'<a class="exp-primary exp-manager-link" href="'+esc(managerUrl)+'">Open parent Experiment workbench ↗</a>'
        :'<p class="exp-hint">Return to the original Space tab to launch or message the agent.</p>')+'</div>';
    return;
  }
  const ready=Boolean(provider&&provider.ready);
  const issue=provider&&provider.issues&&provider.issues.length?provider.issues.join(' · '):'';
  const unavailable=!projectRes.ok?failureText(projectRes)
    :!optionRes.ok?failureText(optionRes)
    :issue;
  el.innerHTML=
    '<div class="exp-copy"><span class="exp-eyebrow">SANDBOX LAB</span>'
    +'<h1>Clone the workspace. Wake an agent.</h1>'
    +'<p>Each sandbox receives filtered working copies of xo-cowork-api and your selected XO project, starts its own Space server, and connects an Agents API executor. Your live folders are never mounted.</p>'
    +'<div class="exp-provider"><span class="exp-dot'+(ready?' is-ready':'')+'"></span>'
    +'<b>'+esc(provider&&provider.label||'Experiment provider')+'</b>'
    +(provider&&provider.production===false?'<span class="exp-dev">local dev</span>':'')+'</div></div>'
    +'<div class="exp-action" '+(launching?'aria-busy="true"':'')+'>'
    +'<label for="exp-project">XO project root</label>'
    +'<div class="exp-controls"><select id="exp-project" '+(!ready||!projects.length||launching?'disabled':'')+'>'
    +projects.map(p=>'<option value="'+esc(p.id)+'" '+(p.id===selected?'selected':'')+'>'
      +esc(p.display_name)+(p.unscaffolded?' · folder':'')+'</option>').join('')
    +'</select><button id="exp-launch" type="button" '+(!ready||!projects.length||launching?'disabled':'')+'>'
    +(launching?'Launching…':'Launch experiment')+'</button></div>'
    +(unavailable?'<p class="exp-warning">'+esc(unavailable)+'</p>':'')
    +(!projects.length&&projectRes.ok?'<p class="exp-warning">No XO projects are available yet.</p>':'')
    +(actionError?'<p class="exp-error exp-actionerror" role="alert">'+esc(actionError)+'</p>':'')
    +'<p class="exp-data-note">Launching allows the OpenAI-hosted agent to read the filtered project and xo-cowork-api copies. Choose only a project you intend to expose to this agent.</p>'
    +'<p class="exp-hint">One active experiment per project. Stop it before launching another.</p></div>';
  document.getElementById('exp-project')?.addEventListener('change',event=>{selected=event.target.value;});
  document.getElementById('exp-launch')?.addEventListener('click',launchSelected);
}

async function refreshOptions(){
  const res=await apiFetch(API_BASE+'/api/experiments/options');
  options=res.ok?res.data:null;
  renderLauncher({ok:true},res);
}

async function launchSelected(){
  if(!projects.some(project=>project.id===selected))selected=projects[0]?.id||'';
  if(!selected||launching)return;
  actionError='';launching=true;renderLauncher();
  const res=await apiFetch(API_BASE+'/api/experiments',{
    method:'POST',body:{project_id:selected}
  });
  launching=false;
  if(!res.ok){
    actionError='Launch failed: '+failureText(res);
    renderLauncher();
    document.getElementById('exp-launch')?.focus();
    toast('launch failed: '+res.error);
    return;
  }
  activeId=res.data.experiment.id;
  toast(res.data.reused?'existing experiment opened':'experiment queued');
  await refreshExperiments();
  renderLauncher();
  const card=[...document.querySelectorAll('.exp-card')]
    .find(element=>element.dataset.id===res.data.experiment.id);
  card?.focus({preventScroll:true});
}

async function refreshExperiments(){
  const res=await apiFetch(API_BASE+'/api/experiments');
  if(res.ok)experiments=res.data.items||[];
  renderExperiments(res);
  renderWorkbench();
}

function renderExperiments(res={ok:true}){
  const el=document.getElementById('exp-list');
  const live=document.getElementById('exp-live');
  if(!el)return;
  if(!res.ok){
    renderKey='';
    el.innerHTML='<div class="exp-note" role="alert">'+esc(failureText(res))+'</div>';
    if(live)live.textContent='Experiment refresh failed';
    return;
  }
  const active=experiments.filter(x=>ACTIVE.has(x.status)).length;
  const newestActive=experiments.find(row=>ACTIVE.has(row.status));
  if(live)live.textContent=newestActive
    ?projectName(newestActive.project_id)+' · '+stageLabel(newestActive.stage)
    :(active?active+' active':'');
  const nextKey=JSON.stringify(experiments)+'|'+[...pendingStops].sort().join(',');
  if(nextKey===renderKey){updateRelativeTimes();return;}
  renderKey=nextKey;
  const focus=focusedControl();
  const scrollPositions=new Map([...el.querySelectorAll('.exp-card')].map(card=>[
    card.dataset.id,card.querySelector('.exp-output')?.scrollTop||0
  ]));
  el.innerHTML=experiments.length?experiments.map(cardHTML).join('')
    :'<div class="exp-empty"><b>No experiments yet</b><span>Choose an XO project and launch its first isolated agent.</span></div>';
  el.querySelectorAll('.exp-stop').forEach(button=>button.addEventListener('click',()=>stop(button.dataset.id,button)));
  el.querySelectorAll('.exp-retry').forEach(button=>button.addEventListener('click',()=>retry(button.dataset.project)));
  el.querySelectorAll('.exp-interact').forEach(button=>button.addEventListener('click',()=>selectExperiment(button.dataset.id)));
  for(const card of el.querySelectorAll('.exp-card')){
    const output=card.querySelector('.exp-output');
    if(output)output.scrollTop=scrollPositions.get(card.dataset.id)||0;
  }
  restoreFocusedControl(focus);
  updateRelativeTimes();
}

function selectExperiment(id){
  if(!experiments.some(row=>row.id===id))return;
  activeId=id;workbenchKey='';renderWorkbench();
  document.getElementById('exp-workbench')?.scrollIntoView({behavior:'smooth',block:'start'});
  document.getElementById('exp-prompt')?.focus({preventScroll:true});
}

function renderWorkbench(){
  const el=document.getElementById('exp-workbench');
  if(!el)return;
  let row=experiments.find(item=>item.id===activeId);
  if(!row){
    row=experiments.find(item=>ACTIVE.has(item.status))||null;
    activeId=row?.id||'';
  }
  if(!row){el.hidden=true;workbenchKey='';return;}
  el.hidden=false;
  if(workbenchKey!==row.id){
    workbenchKey=row.id;
    el.innerHTML='<div class="exp-workhead"><div><span class="exp-eyebrow">LIVE WORKBENCH</span>'
      +'<h2 id="exp-work-title"></h2></div><div class="exp-workactions"><span id="exp-agent-state" class="exp-agent-state"></span>'
      +'<a id="exp-space-link" class="exp-space-link" target="_blank" rel="noopener noreferrer" hidden>Open sandbox Space ↗</a></div></div>'
      +'<div class="exp-workmeta"><span>Agent workspace <code id="exp-workspace"></code></span>'
      +'<span>Sandbox <code id="exp-work-sandbox"></code></span></div>'
      +'<div id="exp-transcript" class="exp-transcript" role="log" aria-live="polite" aria-relevant="additions text"></div>'
      +'<div id="exp-turn-error" class="exp-error" role="alert" hidden></div>'
      +'<form id="exp-prompt-form" class="exp-prompt-form"><label for="exp-prompt">Message this sandbox agent</label>'
      +'<textarea id="exp-prompt" rows="3" maxlength="20000" placeholder="Ask the agent to inspect, change, or run something in the copied project…"></textarea>'
      +'<div class="exp-prompt-foot"><span>Enter sends · Shift+Enter adds a line</span><button id="exp-send" class="exp-primary" type="submit">Send</button></div></form>';
    const textarea=el.querySelector('#exp-prompt');
    textarea.value=draft;
    textarea.addEventListener('input',event=>{
      draft=event.target.value;
      const row=experiments.find(item=>item.id===activeId);
      const send=el.querySelector('#exp-send');
      if(send)send.disabled=!row?.can_message||sending||!draft.trim();
    });
    textarea.addEventListener('keydown',event=>{
      if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendTurn();}
    });
    el.querySelector('#exp-prompt-form').addEventListener('submit',event=>{event.preventDefault();sendTurn();});
  }

  el.querySelector('#exp-work-title').textContent=projectName(row.project_id)+' experiment';
  el.querySelector('#exp-agent-state').textContent=row.turn_status==='running'?'Agent working…':row.status==='ready'?'Agent ready':stageLabel(row.stage);
  el.querySelector('#exp-workspace').textContent=row.workspace_directory||'preparing…';
  el.querySelector('#exp-work-sandbox').textContent=row.sandbox_id?short(row.sandbox_id):'preparing…';

  const link=el.querySelector('#exp-space-link');
  const href=safeHttpUrl(row.space_url);
  link.hidden=!href;
  if(href)link.href=href;
  else link.removeAttribute('href');
  if(href&&announcedReady!==row.id){
    announcedReady=row.id;
    const live=document.getElementById('exp-live');
    if(live)live.textContent=projectName(row.project_id)+' sandbox Space is ready';
  }

  const transcript=el.querySelector('#exp-transcript');
  const messageKey=JSON.stringify(row.messages||[]);
  if(transcript.dataset.key!==messageKey){
    const pinned=transcript.scrollHeight-transcript.scrollTop-transcript.clientHeight<36;
    transcript.dataset.key=messageKey;
    transcript.innerHTML=(row.messages||[]).length?(row.messages||[]).map(messageHTML).join('')
      :'<div class="exp-transcript-empty">The boot check is complete. Send the first task when the agent is ready.</div>';
    if(pinned)transcript.scrollTop=transcript.scrollHeight;
  }

  const error=el.querySelector('#exp-turn-error');
  error.hidden=!row.turn_error;
  error.textContent=row.turn_error||'';
  const textarea=el.querySelector('#exp-prompt');
  const send=el.querySelector('#exp-send');
  const enabled=Boolean(row.can_message)&&!sending;
  textarea.disabled=!enabled;
  send.disabled=!enabled||!draft.trim();
  send.textContent=sending?'Sending…':row.turn_status==='running'?'Working…':'Send';
}

function messageHTML(message){
  const label=message.role==='user'?'You':'Sandbox agent';
  const busy=message.status==='streaming'?'<span class="exp-message-status">working…</span>':'';
  return'<article class="exp-message is-'+esc(message.role)+'"><header><b>'+label+'</b>'+busy+'</header>'
    +'<div>'+esc(message.text||'').replaceAll('\n','<br>')+'</div></article>';
}

async function sendTurn(){
  const row=experiments.find(item=>item.id===activeId);
  const text=draft.trim();
  if(!row||!row.can_message||!text||sending)return;
  sending=true;renderWorkbench();
  const res=await apiFetch(API_BASE+'/api/experiments/'+encodeURIComponent(row.id)+'/turns',{
    method:'POST',body:{text}
  });
  sending=false;
  if(!res.ok){
    actionError='Message failed: '+failureText(res);
    toast('message failed: '+res.error);
    renderLauncher();renderWorkbench();return;
  }
  draft='';actionError='';
  const index=experiments.findIndex(item=>item.id===row.id);
  if(index>=0)experiments[index]=res.data;
  renderLauncher();renderWorkbench();
  const textarea=document.getElementById('exp-prompt');
  if(textarea)textarea.value='';
  toast('task sent to sandbox');
}

function safeHttpUrl(value){
  if(!value)return'';
  try{
    const url=new URL(value,location.origin);
    return url.protocol==='http:'||url.protocol==='https:'?url.href:'';
  }catch{return'';}
}

function cardHTML(row){
  const stateClass=' is-'+esc(row.status);
  const output=row.output?'<pre class="exp-output">'+esc(row.output.trim())+'</pre>':'';
  const error=row.error?'<div class="exp-error">'+esc(row.error)+'</div>':'';
  const refs=(row.sandbox_id||row.agent_session_id)
    ?'<div class="exp-refs">'
      +(row.sandbox_id?'<span>Sandbox <code>'+esc(short(row.sandbox_id))+'</code></span>':'')
      +(row.agent_session_id?'<span>Agent <code>'+esc(short(row.agent_session_id))+'</code></span>':'')+'</div>':'';
  const isStopping=pendingStops.has(row.id)||row.status==='stopping';
  const expiry=row.expires_at?'<div class="exp-refs"><span>Auto-stop <time data-relative="'+esc(row.expires_at)+'" data-future="true">'+relative(row.expires_at,true)+'</time></span></div>':'';
  const spaceUrl=safeHttpUrl(row.space_url);
  return'<article class="exp-card'+stateClass+'" data-id="'+esc(row.id)+'" tabindex="-1">'
    +'<div class="exp-cardhead"><span class="exp-state"><i></i>'+esc(row.status)+'</span>'
    +'<b>'+esc(projectName(row.project_id))+'</b><code class="exp-projectid">'+esc(row.project_id)+'</code>'
    +'<span class="exp-time"><time data-relative="'+esc(row.created_at)+'">'+relative(row.created_at)+'</time></span></div>'
    +progressHTML(row.failed_stage||row.stage,row.status)
    +'<div class="exp-cardbody"><div><span class="exp-label">Current stage</span><strong>'+esc(stageLabel(row.stage))+'</strong></div>'
    +'<div><span class="exp-label">Runtime</span><strong>'+esc(row.model)+' · '+esc(row.provider)+'</strong></div></div>'
    +refs+expiry+output+error
    +'<div class="exp-cardfoot">'
    +(row.status==='ready'||(row.messages||[]).length?'<button class="exp-secondary exp-interact" type="button" data-id="'+esc(row.id)+'">Interact</button>':'')
    +(spaceUrl?'<a class="exp-secondary exp-cardlink" href="'+esc(spaceUrl)+'" target="_blank" rel="noopener noreferrer">Open Space ↗</a>':'')
    +(row.can_stop?'<button class="exp-secondary exp-stop" type="button" data-id="'+esc(row.id)+'" '
      +(isStopping?'disabled':'')+'>'+(isStopping?'Stopping…':'Stop sandbox')+'</button>':'')
    +(row.status==='failed'||row.status==='stopped'?'<button class="exp-primary exp-retry" type="button" data-project="'+esc(row.project_id)+'">Launch again</button>':'')
    +'</div></article>';
}

function progressHTML(stage,status){
  const index=STAGES.findIndex(([key])=>key===stage);
  const failed=status==='failed'||status==='cleanup_failed';
  return'<div class="exp-progress" aria-label="Experiment progress">'+STAGES.map(([key,label],i)=>{
    const cls=failed&&index>=0&&i===index?' is-failed':i<index?' is-done':i===index?' is-current':'';
    const current=i===index&&!failed?' aria-current="step"':'';
    return'<span class="'+cls+'"'+current+'><i></i><small>'+label+'</small></span>';
  }).join('')+'</div>';
}

async function stop(id,button){
  if(pendingStops.has(id))return;
  actionError='';pendingStops.add(id);button.disabled=true;renderExperiments();
  const res=await apiFetch(API_BASE+'/api/experiments/'+encodeURIComponent(id)+'/stop',{method:'POST'});
  pendingStops.delete(id);
  if(!res.ok){actionError='Stop failed: '+failureText(res);renderLauncher();toast('stop failed: '+res.error);}
  else{actionError='';renderLauncher();toast('sandbox stopped');}
  await refreshExperiments();
}

async function retry(projectId){
  selected=projects.some(project=>project.id===projectId)?projectId:(projects[0]?.id||'');
  await launchSelected();
}

function projectName(id){return projects.find(project=>project.id===id)?.display_name||id;}
function stageLabel(stage){return ({
  queued:'Waiting for the provider',creating_session:'Creating Agents API session',
  cloning_project:'Cloning selected project',cloning_cowork_api:'Cloning xo-cowork-api',
  starting_space:'Starting sandbox Space',connecting_agent:'Connecting executor',
  booting_agent:'Running read-only boot check',ready:'Agent connected and idle',
  cleaning_up:'Releasing failed launch resources',stopping:'Releasing sandbox and session',
  stopped:'Resources released',failed:'Launch failed',cleanup_failed:'Cleanup needs attention'
})[stage]||stage.replaceAll('_',' ');}
function short(value){return value.length>24?value.slice(0,12)+'…'+value.slice(-6):value;}
function relative(iso,future=false){
  const seconds=(Date.now()-new Date(iso).getTime())/1000;
  if(!isFinite(seconds))return'';
  if(future){
    const remaining=Math.max(0,-seconds);
    if(remaining<60)return'within a minute';
    if(remaining<3600)return'in '+Math.ceil(remaining/60)+'m';
    return'in '+Math.ceil(remaining/3600)+'h';
  }
  if(seconds<60)return'just now';if(seconds<3600)return Math.floor(seconds/60)+'m ago';
  if(seconds<86400)return Math.floor(seconds/3600)+'h ago';return Math.floor(seconds/86400)+'d ago';
}
function updateRelativeTimes(){
  document.querySelectorAll('#exp-list time[data-relative]').forEach(element=>{
    element.textContent=relative(element.dataset.relative,element.dataset.future==='true');
  });
}
function focusedControl(){
  const element=document.activeElement;
  if(!element||!element.closest('#exp-list'))return null;
  if(element.classList.contains('exp-stop'))return{kind:'stop',id:element.dataset.id};
  if(element.classList.contains('exp-retry'))return{kind:'retry',id:element.dataset.project};
  if(element.classList.contains('exp-interact'))return{kind:'interact',id:element.dataset.id};
  if(element.classList.contains('exp-card'))return{kind:'card',id:element.dataset.id};
  return null;
}
function restoreFocusedControl(focus){
  if(!focus)return;
  const candidates=[...document.querySelectorAll(
    focus.kind==='stop'?'.exp-stop':focus.kind==='retry'?'.exp-retry':focus.kind==='interact'?'.exp-interact':'.exp-card'
  )];
  const match=candidates.find(element=>(
    focus.kind==='retry'?element.dataset.project:element.dataset.id
  )===focus.id);
  if(match&&!match.disabled)match.focus({preventScroll:true});
}
function failureText(res){
  if(res.notImplemented)return'Experiment capability is not installed.';
  if(res.offline)return'xo-cowork-api is unreachable.';
  return res.error||'Experiment request failed.';
}
