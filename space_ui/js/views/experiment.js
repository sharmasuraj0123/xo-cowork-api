/* Experiment panel — one-click OpenAI self-hosted sessions on mini-VPS hosts.
   The browser sends only a project id. The BFF resolves the host path, keeps
   credentials server-side, and exposes a provider-neutral lifecycle snapshot.
   The Chat view provides two mount points: a right rail for launch/history and
   a retained center workbench for interaction. Writes disable while pending;
   one named poll replaces its predecessor. */
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
  ['starting_vps','VPS'],
  ['starting_space','Space'],
  ['connecting_agent','Connect'],
  ['booting_agent','Boot'],
  ['ready','Ready'],
];

let sidebarRoot=null,workbenchRoot=null,projects=[],experiments=[],options=null,selected='',launching=false,visible=false;
let actionError='',renderKey='',activeId='',sending=false,workbenchKey='';
let draft='',announcedReady='',panelActive=false,onActivate=()=>{},onDeactivate=()=>{},onOpenDrawer=()=>{},onCloseDrawer=()=>{};
let navigationRevision=0,stateRevision=0,refreshPromise=null,refreshRequested=false;
const pendingStops=new Set();

export default {
  async mount(sidebarEl,{workbenchEl,activate,deactivate,openDrawer,closeDrawer}={}){
    sidebarRoot=sidebarEl;
    workbenchRoot=workbenchEl;
    onActivate=typeof activate==='function'?activate:()=>{};
    onDeactivate=typeof deactivate==='function'?deactivate:()=>{};
    onOpenDrawer=typeof openDrawer==='function'?openDrawer:()=>{};
    onCloseDrawer=typeof closeDrawer==='function'?closeDrawer:()=>{};
    const requestedId=new URLSearchParams(location.search).get('experiment')||'';
    const openRequested=Boolean(requestedId||document.documentElement.dataset.openExperiment==='true');
    const openRevision=navigationRevision;
    delete document.documentElement.dataset.openExperiment;
    activeId=requestedId||activeId;
    sidebarRoot.innerHTML='<div class="exp exp-rail"><header class="exp-rail-head"><div class="exp-rail-title">'
      +'<span class="exp-rail-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 3v6l-4 8.5A2.4 2.4 0 0 0 6.2 21h11.6a2.4 2.4 0 0 0 2.2-3.5L16 9V3M6.5 13h11M7 3h10"/></svg></span>'
      +'<div><span class="exp-eyebrow">Agents API lab</span><h2>Experiments</h2></div></div>'
      +'<button id="exp-rail-close" class="exp-rail-close" type="button" aria-label="Close Experiments"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15"/></svg></button>'
      +'<p>Launch a self-hosted agent in a disposable, VPS-like workspace.</p>'
      +'<div class="exp-provider exp-provider-global"><span id="exp-provider-dot" class="exp-dot"></span><b id="exp-provider-text">Checking provider…</b><span id="exp-provider-badge" class="exp-dev" hidden></span></div></header>'
      +'<div id="exp-launcher" class="exp-launcher"></div>'
      +'<div class="exp-listhead"><div><span class="exp-list-title">Recent runs</span><span id="exp-count" class="exp-count"></span></div>'
      +'<span id="exp-live" class="exp-live" role="status" aria-live="polite" aria-atomic="true"></span></div>'
      +'<div id="exp-list" class="exp-list"><div class="exp-note">loading experiments…</div></div></div>';
    workbenchRoot.innerHTML='<section id="exp-workbench" class="exp-workbench" hidden></section>';
    sidebarRoot.querySelector('#exp-rail-close')?.addEventListener('click',onCloseDrawer);
    await loadInitial({requestedId,openRequested,openRevision});
  },
  show(){
    visible=true;
    refreshOptions();
    refreshExperiments();
    setSlottedInterval('space-experiments',()=>{if(visible)refreshExperiments();},2000);
  },
  hide(){visible=false;clearSlottedInterval('space-experiments');},
  setActive(active){
    navigationRevision+=1;
    panelActive=Boolean(active);
    renderKey='';
    renderExperiments();
    renderWorkbench();
  },
  activateCurrent(){
    const row=experiments.find(item=>item.id===activeId)
      ||experiments.find(item=>ACTIVE.has(item.status))||experiments[0];
    if(row){activateExperiment(row.id);return true;}
    navigationRevision+=1;
    panelActive=true;
    renderKey='';
    renderExperiments();
    renderWorkbench();
    onActivate('');
    return false;
  }
};

async function loadInitial({requestedId='',openRequested=false,openRevision=0}={}){
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
  const requestedIsValid=!requestedId||experiments.some(row=>row.id===requestedId);
  if(openRequested&&requestedIsValid&&activeId&&navigationRevision===openRevision)activateExperiment(activeId);
}

function renderLauncher(projectRes={ok:true},optionRes={ok:true}){
  const el=sidebarRoot?.querySelector('#exp-launcher');
  if(!el)return;
  if(!projects.some(project=>project.id===selected))selected=projects[0]?.id||'';
  const provider=options&&options.provider;
  const providerDot=sidebarRoot?.querySelector('#exp-provider-dot');
  const providerText=sidebarRoot?.querySelector('#exp-provider-text');
  const providerBadge=sidebarRoot?.querySelector('#exp-provider-badge');
  const managedSandbox=provider&&provider.context==='sandbox';
  const listHead=sidebarRoot?.querySelector('.exp-listhead');
  const list=sidebarRoot?.querySelector('#exp-list');
  if(listHead)listHead.hidden=Boolean(managedSandbox);
  if(list)list.hidden=Boolean(managedSandbox);
  if(providerDot)providerDot.classList.toggle('is-ready',Boolean(provider&&provider.ready));
  if(providerText)providerText.textContent=provider?.label||'Experiment provider';
  if(providerBadge){
    providerBadge.hidden=provider?.production!==false;
    providerBadge.textContent=provider?.production===false?'Local':'Live';
  }
  if(managedSandbox){
    const managerUrl=safeHttpUrl(provider.manager_url);
    el.innerHTML='<div class="exp-copy"><span class="exp-eyebrow">Managed sandbox</span>'
      +'<h3>You are inside an experiment.</h3>'
      +'<p>This Space is the isolated copy for inspecting the selected project. New sandboxes and agent turns are managed by the parent Chat workspace.</p>'
      +'<div class="exp-action"><b>Nested launches are intentionally disabled</b>'
      +'<p class="exp-data-note">The sandbox does not receive your host .env, the Agents API SDK, or Docker access. This protects the host and prevents a sandbox from creating more sandboxes.</p>'
      +(managerUrl?'<a class="exp-primary exp-manager-link" href="'+esc(managerUrl)+'">Open parent Chat workspace ↗</a>'
        :'<p class="exp-hint">Return to the original Space tab to launch or message the agent.</p>')+'</div>';
    return;
  }
  const ready=Boolean(provider&&provider.ready);
  const issue=provider&&provider.issues&&provider.issues.length?provider.issues.join(' · '):'';
  const unavailable=!projectRes.ok?failureText(projectRes)
    :!optionRes.ok?failureText(optionRes)
    :issue;
  el.innerHTML=
    '<div class="exp-launch-head"><div><span class="exp-eyebrow">New experiment</span><h3>Choose a project to launch</h3></div>'
    +'<span class="exp-permission"><i></i>Unrestricted</span></div>'
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
    +'<details class="exp-access"><summary>What can the agent access?<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></summary>'
    +'<p>The agent gets root commands, writable files, package installs, processes, and outbound network access inside its disposable VPS. The host Docker socket and unrelated host files stay outside.</p></details>'
    +'<p class="exp-hint">One active run per project. Stop it before launching another.</p></div>';
  sidebarRoot?.querySelector('#exp-project')?.addEventListener('change',event=>{selected=event.target.value;});
  sidebarRoot?.querySelector('#exp-launch')?.addEventListener('click',launchSelected);
}

async function refreshOptions(){
  const res=await apiFetch(API_BASE+'/api/experiments/options');
  options=res.ok?res.data:null;
  renderLauncher({ok:true},res);
}

async function launchSelected(){
  if(!projects.some(project=>project.id===selected))selected=projects[0]?.id||'';
  if(!selected||launching)return;
  const activationRevision=navigationRevision;
  actionError='';launching=true;renderLauncher();
  const res=await apiFetch(API_BASE+'/api/experiments',{
    method:'POST',body:{project_id:selected}
  });
  launching=false;
  if(!res.ok){
    actionError='Launch failed: '+failureText(res);
    renderLauncher();
    if(!sidebarRoot?.closest('[hidden],[inert]'))sidebarRoot?.querySelector('#exp-launch')?.focus();
    toast('launch failed: '+res.error);
    return;
  }
  const launched=res.data.experiment;
  const shouldActivate=activationRevision===navigationRevision;
  if(shouldActivate)activeId=launched.id;
  upsertExperiment(launched,true);
  stateRevision+=1;
  toast(res.data.reused?'existing experiment opened':'experiment queued');
  renderLauncher();renderExperiments();renderWorkbench();
  if(shouldActivate)activateExperiment(launched.id);
  refreshExperiments();
}

function refreshExperiments(){
  refreshRequested=true;
  if(refreshPromise)return refreshPromise;
  refreshPromise=(async()=>{
    let last={ok:true};
    while(refreshRequested){
      refreshRequested=false;
      const requestRevision=stateRevision;
      const res=await apiFetch(API_BASE+'/api/experiments');
      if(res.ok&&requestRevision===stateRevision)experiments=res.data.items||[];
      renderExperiments(res);
      renderWorkbench();
      last=res;
    }
    return last;
  })().finally(()=>{refreshPromise=null;});
  return refreshPromise;
}

function renderExperiments(res={ok:true}){
  const el=sidebarRoot?.querySelector('#exp-list');
  const live=sidebarRoot?.querySelector('#exp-live');
  const count=sidebarRoot?.querySelector('#exp-count');
  if(!el)return;
  if(!res.ok){
    renderKey='';
    el.innerHTML='<div class="exp-note" role="alert">'+esc(failureText(res))+'</div>';
    setLiveText(live,'Experiment refresh failed');
    return;
  }
  const active=experiments.filter(x=>ACTIVE.has(x.status)).length;
  if(count)count.textContent=experiments.length?experiments.length+' total':'';
  const newestActive=experiments.find(row=>ACTIVE.has(row.status));
  setLiveText(live,newestActive
    ?projectName(newestActive.project_id)+' · '+stageLabel(newestActive.stage)
    :(active?active+' active':''));
  const nextKey=JSON.stringify(experiments)+'|'+[...pendingStops].sort().join(',')
    +'|'+activeId+'|'+panelActive;
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
  el.querySelectorAll('.exp-card').forEach(card=>{
    card.addEventListener('click',event=>{
      if(event.target.closest('button,a,details,summary'))return;
      selectExperiment(card.dataset.id);
    });
    card.addEventListener('keydown',event=>{
      if((event.key==='Enter'||event.key===' ')&&!event.target.closest('button,a,details,summary')){
        event.preventDefault();selectExperiment(card.dataset.id);
      }
    });
  });
  for(const card of el.querySelectorAll('.exp-card')){
    const output=card.querySelector('.exp-output');
    if(output)output.scrollTop=scrollPositions.get(card.dataset.id)||0;
  }
  restoreFocusedControl(focus);
  updateRelativeTimes();
}

function selectExperiment(id){
  if(!experiments.some(row=>row.id===id))return;
  activateExperiment(id);
}

function activateExperiment(id){
  if(!experiments.some(row=>row.id===id))return;
  navigationRevision+=1;
  panelActive=true;
  activeId=id;workbenchKey='';renderKey='';renderExperiments();renderWorkbench();
  onActivate(id);
  requestAnimationFrame(()=>workbenchRoot?.querySelector('#exp-prompt')?.focus({preventScroll:true}));
}

function renderWorkbench(){
  const el=workbenchRoot?.querySelector('#exp-workbench');
  if(!el)return;
  let row=experiments.find(item=>item.id===activeId);
  if(!row){
    row=experiments.find(item=>ACTIVE.has(item.status))||null;
    activeId=row?.id||'';
  }
  if(!row){
    el.hidden=false;
    el.classList.add('is-empty');
    if(workbenchKey!=='__empty__'){
      workbenchKey='__empty__';
      el.innerHTML='<div class="exp-workbench-empty"><div class="exp-empty-orbit" aria-hidden="true"><span></span><i></i><i></i><i></i></div>'
        +'<span class="exp-eyebrow">Disposable execution</span><h2>A clean machine for every idea.</h2>'
        +'<p>Clone any XO project into an isolated VPS, start a self-hosted OpenAI Agents API session, and work with the agent live without touching your host.</p>'
        +'<div class="exp-empty-flow" aria-label="Experiment lifecycle"><span><b>01</b>Project</span><i></i><span><b>02</b>VPS</span><i></i><span><b>03</b>Agent</span></div>'
        +'<button id="exp-empty-launch" class="exp-primary" type="button">Choose a project</button>'
        +'<small>Unrestricted inside the disposable machine · host stays isolated</small></div>';
      el.querySelector('#exp-empty-launch').addEventListener('click',onOpenDrawer);
    }
    return;
  }
  el.hidden=false;
  el.classList.remove('is-empty');
  if(workbenchKey!==row.id){
    workbenchKey=row.id;
    el.innerHTML='<div class="exp-workhead"><div class="exp-workidentity"><button id="exp-back-chat" class="exp-back-chat" type="button"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12 5-5 5 5 5"/></svg>Back to chat</button>'
      +'<span class="exp-eyebrow">Live workbench</span><h2 id="exp-work-title"></h2></div>'
      +'<div class="exp-workactions"><span id="exp-agent-state" class="exp-agent-state"><i></i><span></span></span>'
      +'<a id="exp-app-link" class="exp-space-link exp-tool-link exp-app-link" target="_blank" rel="noopener noreferrer" hidden>App ↗</a>'
      +'<a id="exp-vps-link" class="exp-space-link exp-tool-link exp-vps-link" target="_blank" rel="noopener noreferrer" hidden>VPS ↗</a>'
      +'<a id="exp-space-link" class="exp-space-link exp-main-link" target="_blank" rel="noopener noreferrer" hidden>Open Space ↗</a></div></div>'
      +'<div class="exp-workmeta"><span><b>Agent workspace</b><code id="exp-workspace"></code></span>'
      +'<span><b>VPS instance</b><code id="exp-work-sandbox"></code></span></div>'
      +'<div id="exp-transcript" class="exp-transcript" role="log" aria-live="polite" aria-relevant="additions text"></div>'
      +'<div id="exp-turn-error" class="exp-error" role="alert" hidden></div>'
      +'<form id="exp-prompt-form" class="exp-prompt-form"><label for="exp-prompt">Message this self-hosted agent</label>'
      +'<textarea id="exp-prompt" rows="3" maxlength="20000" placeholder="Ask the agent to inspect, change, or run something in the copied project…"></textarea>'
      +'<div class="exp-prompt-foot"><span>Enter sends · Shift+Enter adds a line</span><button id="exp-send" class="exp-primary" type="submit">Send</button></div></form>';
    const textarea=el.querySelector('#exp-prompt');
    el.querySelector('#exp-back-chat').addEventListener('click',onDeactivate);
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
  el.querySelector('#exp-agent-state span').textContent=row.turn_status==='running'?'Agent working…':row.status==='ready'?'Agent ready':stageLabel(row.stage);
  el.querySelector('#exp-agent-state').classList.toggle('is-busy',row.turn_status==='running'||row.status==='starting');
  el.querySelector('#exp-workspace').textContent=row.workspace_directory||'preparing…';
  el.querySelector('#exp-work-sandbox').textContent=row.sandbox_id?short(row.sandbox_id):'preparing…';

  const appLink=el.querySelector('#exp-app-link');
  const appHref=safeHttpUrl(row.app_url);
  appLink.hidden=!appHref;
  if(appHref)appLink.href=appHref;
  else appLink.removeAttribute('href');

  const vpsLink=el.querySelector('#exp-vps-link');
  const vpsHref=safeHttpUrl(row.vps_url);
  vpsLink.hidden=!vpsHref;
  if(vpsHref)vpsLink.href=vpsHref;
  else vpsLink.removeAttribute('href');

  const link=el.querySelector('#exp-space-link');
  const href=safeHttpUrl(row.space_url);
  link.hidden=!href;
  if(href)link.href=href;
  else link.removeAttribute('href');
  if(href&&announcedReady!==row.id){
    announcedReady=row.id;
    const live=sidebarRoot?.querySelector('#exp-live');
    setLiveText(live,projectName(row.project_id)+' sandbox Space is ready');
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
  const label=message.role==='user'?'You':'Self-hosted agent';
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
  upsertExperiment(res.data);
  stateRevision+=1;
  renderLauncher();renderExperiments();renderWorkbench();
  const textarea=workbenchRoot?.querySelector('#exp-prompt');
  if(textarea)textarea.value='';
  toast('task sent to self-hosted agent');
}

function safeHttpUrl(value){
  if(!value)return'';
  try{
    const url=new URL(value,location.origin);
    return url.protocol==='http:'||url.protocol==='https:'?url.href:'';
  }catch{return'';}
}

function cardHTML(row){
  const stateClass=' is-'+esc(row.status)+(panelActive&&row.id===activeId?' is-selected':'');
  const output=row.output?'<details class="exp-output-wrap"><summary>Agent output</summary><pre class="exp-output">'+esc(row.output.trim())+'</pre></details>':'';
  const error=row.error?'<div class="exp-error">'+esc(row.error)+'</div>':'';
  const refs=(row.sandbox_id||row.agent_session_id)
    ?'<div class="exp-refs">'
      +(row.sandbox_id?'<span>VPS <code>'+esc(short(row.sandbox_id))+'</code></span>':'')
      +(row.agent_session_id?'<span>Agent <code>'+esc(short(row.agent_session_id))+'</code></span>':'')+'</div>':'';
  const isStopping=pendingStops.has(row.id)||row.status==='stopping';
  const expiry=row.expires_at?'<div class="exp-refs"><span>Auto-stop <time data-relative="'+esc(row.expires_at)+'" data-future="true">'+relative(row.expires_at,true)+'</time></span></div>':'';
  const spaceUrl=safeHttpUrl(row.space_url);
  const vpsUrl=safeHttpUrl(row.vps_url);
  return'<article class="exp-card'+stateClass+'" data-id="'+esc(row.id)+'" tabindex="0" aria-label="Open '+esc(projectName(row.project_id))+' experiment">'
    +'<div class="exp-cardhead"><span class="exp-state"><i></i>'+esc(row.status)+'</span><span class="exp-time"><time data-relative="'+esc(row.created_at)+'">'+relative(row.created_at)+'</time></span></div>'
    +'<div class="exp-cardtitle"><b>'+esc(projectName(row.project_id))+'</b><code class="exp-projectid">'+esc(row.project_id)+'</code></div>'
    +'<div class="exp-stage"><span><small>Current stage</small><strong>'+esc(stageLabel(row.stage))+'</strong></span>'+progressHTML(row.failed_stage||row.stage,row.status)+'</div>'
    +'<details class="exp-carddetails"><summary>Run details <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></summary>'
    +'<div class="exp-cardbody"><div><span class="exp-label">Runtime</span><strong>'+esc(row.model)+'</strong></div>'
    +'<div><span class="exp-label">Access</span><strong>'+esc(row.permission_profile||'unrestricted')+' · '+esc(row.provider)+'</strong></div></div>'
    +refs+expiry+output+error+'</details>'
    +'<div class="exp-cardfoot">'
    +(row.status==='ready'||(row.messages||[]).length?'<button class="exp-primary exp-interact" type="button" data-id="'+esc(row.id)+'" aria-pressed="'+String(panelActive&&row.id===activeId)+'">Open workbench</button>':'')
    +(spaceUrl?'<a class="exp-secondary exp-cardlink" href="'+esc(spaceUrl)+'" target="_blank" rel="noopener noreferrer">Open Space ↗</a>':'')
    +(vpsUrl?'<a class="exp-secondary exp-cardlink exp-vps-cardlink" href="'+esc(vpsUrl)+'" target="_blank" rel="noopener noreferrer">VPS ↗</a>':'')
    +(row.can_stop?'<button class="exp-secondary exp-stop is-danger" type="button" data-id="'+esc(row.id)+'" '
      +(isStopping?'disabled':'')+'>'+(isStopping?'Stopping…':'Stop VPS')+'</button>':'')
    +(row.status==='failed'||row.status==='stopped'?'<button class="exp-primary exp-retry" type="button" data-project="'+esc(row.project_id)+'">Launch again</button>':'')
    +'</div></article>';
}

function progressHTML(stage,status){
  let index=STAGES.findIndex(([key])=>key===stage);
  if(index<0&&status==='stopped')index=STAGES.length;
  else if(index<0&&(status==='stopping'||status==='cleanup_failed'))index=STAGES.length-1;
  const failed=status==='failed'||status==='cleanup_failed';
  return'<div class="exp-progress" aria-label="Experiment progress">'+STAGES.map(([key,label],i)=>{
    const cls=failed&&index>=0&&i===index?' is-failed':i<index||status==='stopped'?' is-done':i===index?' is-current':'';
    const current=i===index&&!failed?' aria-current="step"':'';
    return'<span class="'+cls+'"'+current+' title="'+esc(label)+'"><i></i><small>'+label+'</small></span>';
  }).join('')+'</div>';
}

async function stop(id,button){
  if(pendingStops.has(id))return;
  actionError='';pendingStops.add(id);button.disabled=true;renderExperiments();
  const res=await apiFetch(API_BASE+'/api/experiments/'+encodeURIComponent(id)+'/stop',{method:'POST'});
  pendingStops.delete(id);
  if(!res.ok){actionError='Stop failed: '+failureText(res);renderLauncher();toast('stop failed: '+res.error);}
  else{
    actionError='';upsertExperiment(res.data);stateRevision+=1;
    renderLauncher();renderExperiments();renderWorkbench();toast('VPS stopped');
  }
  await refreshExperiments();
}

async function retry(projectId){
  selected=projects.some(project=>project.id===projectId)?projectId:(projects[0]?.id||'');
  await launchSelected();
}

function projectName(id){return projects.find(project=>project.id===id)?.display_name||id;}
function upsertExperiment(snapshot,prepend=false){
  const index=experiments.findIndex(row=>row.id===snapshot.id);
  if(index>=0)experiments[index]=snapshot;
  else if(prepend)experiments.unshift(snapshot);
  else experiments.push(snapshot);
}
function setLiveText(element,value){if(element&&element.textContent!==value)element.textContent=value;}
function stageLabel(stage){return ({
  queued:'Waiting for the provider',creating_session:'Creating Agents API session',
  cloning_project:'Cloning selected project',cloning_cowork_api:'Cloning xo-cowork-api',
  starting_vps:'Starting self-hosted VPS',
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
  sidebarRoot?.querySelectorAll('#exp-list time[data-relative]').forEach(element=>{
    element.textContent=relative(element.dataset.relative,element.dataset.future==='true');
  });
}
function focusedControl(){
  const element=document.activeElement;
  if(!element||!sidebarRoot?.contains(element)||!element.closest('#exp-list'))return null;
  if(element.classList.contains('exp-stop'))return{kind:'stop',id:element.dataset.id};
  if(element.classList.contains('exp-retry'))return{kind:'retry',id:element.dataset.project};
  if(element.classList.contains('exp-interact'))return{kind:'interact',id:element.dataset.id};
  if(element.classList.contains('exp-card'))return{kind:'card',id:element.dataset.id};
  return null;
}
function restoreFocusedControl(focus){
  if(!focus)return;
  const candidates=[...(sidebarRoot?.querySelectorAll(
    focus.kind==='stop'?'.exp-stop':focus.kind==='retry'?'.exp-retry':focus.kind==='interact'?'.exp-interact':'.exp-card'
  )||[])];
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
