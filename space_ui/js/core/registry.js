/* View registry: builds the tab nav from registered views, assigns hotkeys
   1..n, syncs the URL hash (#/<id>, deep-linkable), lazy-mounts each view on
   first activation, and isolates a view's failure to its own section — the
   other tabs keep working. The registry knows the view contract, never the
   views themselves (the same seam philosophy as the backend's capability
   loader).

   View contract (js/views/*.js default export or named export):
     {
       id: 'sessions',          // section is #view-<id>, tab is #tab-<id>
       label: 'Sessions',       // tab text (may contain entities)
       order: 4,                // nav position; hotkey is its 1-based index
       async mount(el, ctx) {}, // first activation; el is the section
       show() {}, hide() {},    // optional, on tab switches
     }
   The section is created inside #stage automatically when index.html does
   not already carry one — markup-heavy views keep theirs in index.html,
   render-everything views need no HTML edit at all.
   ctx = {switchTo}. Views never import each other; cross-view jumps go
   through ctx.switchTo(id). */

let views=[];
const byId=new Map();
let current=null;

export function registerView(v){
  if(byId.has(v.id))views=views.map(w=>w.id===v.id?v:w); /* idempotent re-register */
  else views.push(v);
  byId.set(v.id,v);
}

const ctx={switchTo};

export async function switchTo(id){
  const v=byId.get(id);
  if(!v)return;
  const prev=current&&current!==id?byId.get(current):null;
  current=id;
  document.body.dataset.view=id;
  for(const w of views){
    document.getElementById('view-'+w.id)?.classList.toggle('is-active',w.id===id);
    /* a tabless view (hideTab) lights up its parentTab so the topbar still
       says where you are (e.g. Six Degrees lives inside Timeline) */
    document.getElementById('tab-'+w.id)?.classList.toggle('is-on',w.id===id||v.parentTab===w.id);
  }
  document.getElementById('tab-'+id)?.scrollIntoView({block:'nearest',inline:'nearest'});
  history.replaceState(null,'','#/'+id);
  if(prev&&prev.hide){
    try{prev.hide();}catch(err){console.error('view "'+prev.id+'" hide failed:',err);}
  }
  if(!v.mounted){
    v.mounted=true; /* idempotent mount: activating N times mounts once */
    const el=document.getElementById('view-'+v.id);
    try{await v.mount(el,ctx);}
    catch(err){
      console.error('view "'+v.id+'" failed to mount:',err);
      renderMountError(el,v);
      return;
    }
  }
  if(v.show){
    try{v.show();}catch(err){console.error('view "'+v.id+'" show failed:',err);}
  }
}

export function startRegistry({defaultView}){
  views.sort((a,b)=>(a.order||0)-(b.order||0));
  const stage=document.getElementById('stage');
  for(const v of views){
    if(stage&&!document.getElementById('view-'+v.id)){
      const s=document.createElement('section');
      s.className='view';s.id='view-'+v.id;
      stage.appendChild(s);
    }
  }
  /* hideTab views stay registered (sections, hash deep-links, switchTo) but
     get no tab button and no digit hotkey — they are reached from inside
     another view (e.g. Six Degrees from the Timeline header) */
  const tabbed=views.filter(v=>!v.hideTab);
  const tabs=document.querySelector('.tabs');
  if(tabs)tabs.replaceChildren(...tabbed.map(v=>{
    const b=document.createElement('button');
    b.id='tab-'+v.id;
    b.innerHTML=v.label;
    b.addEventListener('click',()=>switchTo(v.id));
    return b;
  }));
  addEventListener('keydown',e=>{
    if(/INPUT|TEXTAREA/.test(document.activeElement?.tagName||''))return;
    if(e.key.length!==1||e.key<'1'||e.key>'9')return;
    const i=e.key.charCodeAt(0)-49;
    if(i<tabbed.length)switchTo(tabbed[i].id);
  });
  addEventListener('hashchange',()=>{
    const id=location.hash.replace(/^#\//,'');
    if(byId.has(id)&&id!==current)switchTo(id);
  });
  const initial=location.hash.replace(/^#\//,'');
  switchTo(byId.has(initial)?initial:defaultView);
}

/* per-view bulkhead: a throwing mount gets an error card in its own section */
function renderMountError(el,v){
  if(!el)return;
  const box=document.createElement('div');
  box.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:32px';
  box.innerHTML='<div>'
    +'<div style="font:400 10px ui-monospace,monospace;letter-spacing:.14em;color:#7d786d">VIEW FAILED</div>'
    +'<p style="max-width:44ch;color:#b3ada0;font-size:14px;margin:10px 0 0">The '+v.label
    +' view hit an error and was isolated. The other tabs keep working. Details are in the browser console.</p>'
    +'</div>';
  el.appendChild(box);
}
