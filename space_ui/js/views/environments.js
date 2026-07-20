/* Whiteboard — the Environments dashboard as a composable canvas (zero deps).
   Widget cards (work items, teammates, clusters, status, events), sticky
   notes, and embeds (HTML files / iframes) sit on a pannable/zoomable
   surface. Drag a card by its header; resize from its corner handle; every
   component carries its own "?" note explaining what it is and how it
   changes. Layout, notes, and embeds persist in localStorage only — .xo/
   stays watcher-owned and untouched.

   Efficiency notes: one pointer handler pair per board (pan + drag + resize
   share it), all motion is CSS transforms/width/height on the dragged node
   only, saves are debounced with a pagehide flush, data is fetched once per
   mount (server caches 30s). Data: GET data/overview.json +
   data/environments_graph.json. */
import {apiFetch} from '../core/api.js';
import {renderFilesList} from '../core/fileslist.js';

const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const rel=iso=>{
  if(!iso)return '—';
  const s=(Date.now()-+new Date(iso))/1000;
  if(s<90)return 'just now';
  if(s<3600)return Math.round(s/60)+'m ago';
  if(s<86400)return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
};

const STORE_KEY='space.envboard.v1';
const load=()=>{
  /* a stored primitive (corrupt key) must degrade to {} — not crash mount */
  try{const v=JSON.parse(localStorage.getItem(STORE_KEY));
    return (v&&typeof v==='object'&&!Array.isArray(v))?v:{};}catch(_e){return {};}
};
let saveT=null,savePending=null;
const flushSave=()=>{
  if(!savePending)return;
  clearTimeout(saveT);
  try{localStorage.setItem(STORE_KEY,JSON.stringify(savePending));}catch(_e){}
  savePending=null;
};
const save=state=>{
  savePending=state;
  clearTimeout(saveT);saveT=setTimeout(flushSave,250);
};
/* the debounce window must not lose the last edit on tab close/navigation */
addEventListener('pagehide',flushSave);

/* Built-in card kinds. 'guide' is the whiteboard's explainer: note-styled,
   permanent (no remove button), but draggable and resizable like everything
   else. Default positions are NOT static — the board divides into rough
   viewport quadrants (see quadrantLayout()): Q1 top-left is the Sessions
   dashboard at full quadrant size, Q4 bottom-right the Projects dashboard,
   and the rest arrange across Q2 (top-right) and Q3 (bottom-left). */
const CARD_DEFS=[
  {id:'guide',    title:'How this works',note:true,permanent:true},
  {id:'status',   title:'Environment'},
  {id:'clusters', title:'Clusters'},
  {id:'work',     title:'Work items'},
  {id:'team',     title:'Teammates'},
  {id:'events',   title:'Recent events'},
  /* the other spaces' dashboards, hosted live as cards (see app.js, which
     hands the view modules over via environmentsView.sections) */
  {id:'projects-dash',title:'Projects dashboard',section:'projects'},
  {id:'sessions-dash',title:'Sessions dashboard',section:'sessions'},
  /* the Files tab's List view, hosted as a card (core/fileslist.js) */
  {id:'files-list',   title:'Files',files:true},
];

/* Rough viewport quadrants for the landing layout. Saved positions always
   win; this only shapes fresh boards and Reset layout. */
function quadrantLayout(board){
  const W=Math.max(board?.clientWidth||0,1000);
  const H=Math.max(board?.clientHeight||0,640);
  const g=16,TOP=48;               /* gutter + toolbar clearance */
  const qw=W/2,qh=H/2;
  const colW=(qw-4*g)/3;           /* Q2: three equal columns */
  const halfW=(qw-3*g)/2;          /* Q3: 2x2 grid cells */
  const halfH=(qh-3*g)/2;
  return {
    /* Q1 — the Sessions dashboard owns the entire first quadrant */
    'sessions-dash':{x:g,        y:TOP,          w:qw-2*g, h:qh-TOP-g},
    /* Q4 — the Projects dashboard owns the bottom-right quadrant */
    'projects-dash':{x:qw+g,     y:qh+g,         w:qw-2*g, h:qh-2*g},
    /* Q2 (top-right): guide · environment · clusters as columns */
    guide:          {x:qw+g,     y:TOP,          w:colW,   h:qh-TOP-g},
    status:         {x:qw+2*g+colW,   y:TOP,     w:colW,   h:qh-TOP-g},
    clusters:       {x:qw+3*g+2*colW, y:TOP,     w:colW,   h:qh-TOP-g},
    /* Q3 (bottom-left): work · teammates / events · files as a 2x2 grid */
    work:           {x:g,        y:qh+g,         w:halfW,  h:halfH},
    team:           {x:2*g+halfW,y:qh+g,         w:halfW,  h:halfH},
    events:         {x:g,        y:qh+2*g+halfH, w:halfW,  h:halfH},
    'files-list':   {x:2*g+halfW,y:qh+2*g+halfH, w:halfW,  h:halfH},
  };
}
const NOTE_W=240;
const EMBED_W=460,EMBED_H=320;

/* Per-component notes: what each card is, and what makes it change. */
const CARD_INFO={
  status:'This workspace as a live environment: host, root path, project '
    +'count, server status. It follows the watcher’s workspace.json; '
    +'the ↻ toolbar button re-reads it.',
  clusters:'The five business-purpose clusters from the environments '
    +'classifier. Counts shift when projects gain app manifests, slide '
    +'decks, docs-site configs, contract paperwork, or a manual "category" '
    +'tag in their .xo/project.json.',
  work:'Work items from the watcher’s todos.json. Until todos are '
    +'collected it shows the latest activity per project instead. Updates '
    +'as agent sessions record work.',
  team:'Agents in live sessions (green dot) plus runtimes that are known '
    +'but idle. Follows .xo/activity.json as sessions open and close.',
  events:'The newest entries of .xo/timeline.jsonl: file edits and session '
    +'starts, as they happen across the workspace.',
  'projects-dash':'The Projects space’s full dashboard, live on this board: '
    +'workspace projects with their status and actions. Same view you get '
    +'from the Projects space’s Dashboard tab.',
  'sessions-dash':'The Sessions space’s full telemetry dashboard, live on '
    +'this board: tokens, models, tools, and per-session detail. Same view '
    +'as the Sessions space’s Dashboard tab; its window and source filters '
    +'work here too.',
  'files-list':'The workspace’s file contents as a collapsible tree — the '
    +'same component the Files tab shows in List mode. Rebuilt from disk '
    +'behind a 30s server cache; the ↻ toolbar button re-reads it.',
};

/* embeds may point at served paths or http(s) URLs — never script: URLs */
const safeSrc=s=>{
  const t=String(s||'').trim();
  if(!t||/^[a-z]+script:/i.test(t)||t.startsWith('data:'))return null;
  return t;
};

export default {
  id:'environments',label:'Environments',order:0,
  async mount(el,ctx){
    /* other spaces' dashboard views, injected by app.js (the composition
       seam) as environmentsView.sections — registry calls mount(el,ctx) as
       a method, so `this` is the exported view object */
    const sections=(this&&this.sections)||{};
    el.classList.add('envhost');
    el.innerHTML=`
      <div class="btoolbar">
        <span class="beye">whiteboard</span>
        <button id="bd-note" title="Add a sticky note">+ Note</button>
        <button id="bd-html" title="Load an HTML file onto the board">+ HTML</button>
        <button id="bd-embed" title="Embed a page by URL">+ Embed</button>
        <button id="bd-fit" title="Reset pan and zoom">Fit</button>
        <button id="bd-reset" title="Restore default layout (keeps notes and embeds)">Reset layout</button>
        <button id="bd-refresh" title="Re-fetch data">&#8635;</button>
        <span class="bhidden" id="bd-hidden"></span>
      </div>
      <div class="board" id="envboard"><div class="bsurface" id="bsurface"></div></div>`;
    const board=el.querySelector('#envboard');
    const surface=el.querySelector('#bsurface');

    const state=load();
    state.cards=state.cards||{};
    state.notes=state.notes||{};
    state.embeds=state.embeds||{};
    state.cam=state.cam||{x:0,y:0,k:1};
    const cam=state.cam;

    const applyCam=()=>{surface.style.transform=`translate(${cam.x}px,${cam.y}px) scale(${cam.k})`;};
    applyCam();

    /* Fit: frame every visible card (never zoom past 100%). The board lands
       on this by default — a saved camera from a past session can otherwise
       open onto empty canvas. */
    function fitBoard(){
      const cards=[...surface.querySelectorAll('.bcard')];
      if(!cards.length){cam.x=0;cam.y=0;cam.k=1;applyCam();return;}
      let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
      for(const c of cards){
        x0=Math.min(x0,c._px||0);y0=Math.min(y0,c._py||0);
        x1=Math.max(x1,(c._px||0)+c.offsetWidth);y1=Math.max(y1,(c._py||0)+c.offsetHeight);
      }
      const W=board.clientWidth||1200,H=board.clientHeight||700;
      const p=24,topPad=44;   /* breathing room; toolbar clearance up top */
      const k=Math.min(1,(W-2*p)/Math.max(1,x1-x0),(H-topPad-p)/Math.max(1,y1-y0));
      cam.k=Math.max(.4,k);
      cam.x=(W-(x1-x0)*cam.k)/2-x0*cam.k;
      cam.y=topPad+((H-topPad-p)-(y1-y0)*cam.k)/2-y0*cam.k;
      applyCam();save(state);
    }

    /* ---------- data ---------- */
    let D=null,G=null;
    async function fetchData(){
      const [o,g]=await Promise.all([
        apiFetch('data/overview.json'),
        apiFetch('data/environments_graph.json'),
      ]);
      D=o.ok?o.data:null;
      G=g.ok?g.data:null;
    }

    /* ---------- card content builders (innerHTML-safe: all esc'd) ---------- */
    const row=(main,meta,time,live)=>`<div class="xrow">${live!=null?`<span class="xdot${live?' live':''}"></span>`:''}
      <span class="xr-main">${main}</span><span class="xr-meta">${meta}</span><span class="xr-time">${time}</span></div>`;

    function bodyFor(id){
      if(id==='guide')return `
        <p class="bguide">This is your whiteboard: every card is live
        workspace data. <b>Drag</b> a card by its header; <b>resize</b> it
        from its corner handle. <b>Drag</b> the dotted canvas to pan,
        <b>scroll</b> to zoom. The <b>?</b> on each card explains what it
        shows. Add <b>sticky notes</b>, <b>HTML files</b>, or <b>embeds</b>
        from the toolbar; removed cards wait there too. Your layout saves on
        this machine automatically.</p>`;
      if(!D&&id!=='clusters')return '<div class="xempty">Data unavailable.</div>';
      if(id==='status'){
        const ws=D.workspace||{};
        return `<div class="xkv"><span>Host</span><b>local · this machine</b></div>
          <div class="xkv"><span>Root</span><b class="mono">${esc(D.root||'—')}</b></div>
          <div class="xkv"><span>Projects</span><b>${(ws.projects||[]).length}</b></div>
          <div class="xkv"><span>Status</span><span class="xpill is-on">online</span></div>
          <div class="xfoot">updated ${rel(ws.updated_at)}</div>`;
      }
      if(id==='clusters'){
        if(!G)return '<div class="xempty">Cluster data unavailable.</div>';
        const colors=G.categories||{};
        return (G.hubs||[]).map(h=>`<div class="xrow">
          <span class="xdot" style="background:${esc((colors[h.cat]||{}).color||'#888')}"></span>
          <span class="xr-main">${esc(h.label)}</span>
          <span class="xr-meta">${esc(h.blurb||'')}</span>
          <button class="xgo" data-goto="${esc(h.id)}" title="Show in Files graph">&rarr;</button>
        </div>`).join('');
      }
      if(id==='work'){
        /* .xo/todos.json real shape: {schema, updated_at, sessions:{<sid>:
           {todos:[...]}}} — but accept plain arrays and {items} too */
        let todos=Array.isArray(D.todos)?D.todos:(D.todos&&D.todos.items)||null;
        if(!todos&&D.todos&&D.todos.sessions)
          todos=Object.values(D.todos.sessions).flatMap(s=>(s&&s.todos)||[]);
        if(todos&&todos.length)return todos.slice(0,14).map(t=>row(
          esc(t.title||t.text||t.content||String(t)),esc(t.project_id||t.project||''),esc(t.status||''),
          !(t.done||t.status==='done'))).join('');
        const seen=new Map();
        for(const e of D.timeline||[]){
          if(!e.project_id||seen.has(e.project_id))continue;
          seen.set(e.project_id,e);
          if(seen.size>=10)break;
        }
        const rows=[...seen.values()].map(e=>row(esc(e.project_id),esc(e.type||''),rel(e.ts))).join('');
        return rows?`<div class="xfoot">No work items collected yet — recent activity per project:</div>${rows}`
          :'<div class="xempty">No work items collected yet.</div>';
      }
      if(id==='team'){
        const act=D.activity||{},st=D.stats||{};
        const open=act.open_sessions||[];
        const runtimes=Object.keys(st.by_runtime||{});
        return (open.map(s=>row(esc(s.agent||s.runtime||'agent'),
            `${esc(s.runtime||'')} · ${esc(s.project_id||'')}`,rel(s.last_activity_at),true)).join('')
          +runtimes.filter(r=>!open.some(s=>s.runtime===r))
            .map(r=>row(esc(r),'runtime · idle','',false)).join(''))
          ||'<div class="xempty">No agents active.</div>';
      }
      if(id==='events'){
        return (D.timeline||[]).slice(0,16).map(e=>row(
          esc(e.type||'event'),`${esc(e.project_id||'')}${e.path?' · '+esc(e.path):''}`,rel(e.ts))).join('')
          ||'<div class="xempty">No recent events.</div>';
      }
      return '';
    }

    /* ---------- card DOM ---------- */
    function makeCard(id,title,pos,opts={}){
      const c=document.createElement('div');
      c.className='bcard'+(opts.note?' bnote':'')+(opts.embed?' bembed':'')+(opts.dash?' bdash':'');
      c.dataset.id=id;
      c.style.width=(pos.w||NOTE_W)+'px';
      if(pos.h){c.style.height=pos.h+'px';c.style.maxHeight='none';}
      /* numeric position lives on the element (never parsed back out of the
         CSS transform string — browsers may serialize exponents there) */
      c._px=pos.x;c._py=pos.y;
      c.style.transform=`translate(${pos.x}px,${pos.y}px)`;
      c.innerHTML=`<div class="bhead">
          <span class="bttl" title="${esc(title)}">${esc(title)}</span>
          ${CARD_INFO[id]?'<button class="bi" title="What is this card?">?</button>':''}
          <button class="bmin" title="Collapse">&#8722;</button>
          ${opts.permanent?'':'<button class="bx" title="Remove from board">&#10005;</button>'}
        </div>
        ${CARD_INFO[id]?`<div class="binfo" ${!pos.info?'hidden':''}>${esc(CARD_INFO[id])}</div>`:''}
        <div class="bbody"></div>
        <div class="brs" title="Resize"></div>`;
      if(pos.collapsed)c.classList.add('is-min');
      surface.appendChild(c);
      return c;
    }

    function renderCards(){
      surface.innerHTML='';
      const L=quadrantLayout(board);  /* fresh each render: tracks viewport */
      for(const def of CARD_DEFS){
        const st=state.cards[def.id]||{};
        if(st.hidden&&!def.permanent)continue;
        const q=L[def.id]||{x:40,y:48,w:300};
        const c=makeCard(def.id,def.title,
          {x:st.x??q.x,y:st.y??q.y,w:st.w??q.w,h:st.h??q.h,collapsed:st.collapsed,info:st.info},
          {note:def.note,permanent:def.permanent,dash:!!def.section||def.files});
        const body=c.querySelector('.bbody');
        if(def.files){
          /* same renderer as Files → List; failure stays inside the card */
          renderFilesList(body).catch(err=>{
            console.error('whiteboard: files list failed:',err);
            body.innerHTML='<div class="xempty">Contents unavailable.</div>';
          });
          continue;
        }
        if(def.section){
          /* a whole space dashboard, mounted live inside the card. Failure
             stays inside this card — the board must keep working. */
          const view=sections[def.section];
          if(!view){body.innerHTML='<div class="xempty">Dashboard unavailable.</div>';continue;}
          if(def.section==='sessions'){
            /* sessions.js renders into a global #sesswrap; in this space
               the card owns the only one (app.js removed the section's) */
            body.innerHTML='<div class="sesswrap" id="sesswrap"></div>';
          }
          /* full view lifecycle: mount builds, show() triggers the first
             load+render (that's the registry's contract too) */
          Promise.resolve()
            .then(()=>view.mount(body,ctx))
            .then(()=>{if(view.show)view.show();})
            .catch(err=>{
              console.error('whiteboard: '+def.section+' dashboard failed:',err);
              body.innerHTML='<div class="xempty">Dashboard failed to mount.</div>';
            });
          continue;
        }
        body.innerHTML=bodyFor(def.id);
      }
      let ni=0;
      for(const [nid,txt] of Object.entries(state.notes)){
        const st=state.cards[nid]||{};
        if(st.hidden)continue;
        /* default positions cascade so reset notes never stack exactly */
        const c=makeCard(nid,'Note',
          {x:st.x??(120+ni*32),y:st.y??(760+ni*28),w:st.w,h:st.h,collapsed:st.collapsed},{note:true});
        ni++;
        const body=c.querySelector('.bbody');
        /* Firefox < 136 throws SyntaxError on 'plaintext-only' — fall back
           to plain contenteditable; the textContent write below and the
           innerText read on input keep note text plain either way */
        try{body.contentEditable='plaintext-only';}catch(_e){body.contentEditable='true';}
        body.textContent=txt;           /* plain text only — never innerHTML */
        body.addEventListener('input',()=>{state.notes[nid]=body.innerText;save(state);});
      }
      let ei=0;
      for(const [eid,emb] of Object.entries(state.embeds)){
        const st=state.cards[eid]||{};
        if(st.hidden)continue;
        const src=safeSrc(emb.src);
        const c=makeCard(eid,emb.title||src||'Embed',
          {x:st.x??(460+ei*36),y:st.y??(780+ei*30),w:st.w??EMBED_W,h:st.h??EMBED_H,collapsed:st.collapsed},
          {embed:true});
        ei++;
        const body=c.querySelector('.bbody');
        if(!src){body.innerHTML='<div class="xempty">Blocked source.</div>';continue;}
        const f=document.createElement('iframe');
        /* sandboxed: embedded pages can run their own scripts but never
           reach this page (no allow-same-origin) */
        f.setAttribute('sandbox','allow-scripts allow-popups');
        f.loading='lazy';
        f.src=src;
        body.appendChild(f);
      }
      renderHiddenChips();
    }

    function renderHiddenChips(){
      const holder=el.querySelector('#bd-hidden');
      const hidden=CARD_DEFS.filter(d=>!d.permanent&&(state.cards[d.id]||{}).hidden);
      holder.innerHTML=hidden.length?'Hidden: '+hidden.map(d=>
        `<button data-show="${d.id}">+ ${esc(d.title)}</button>`).join(''):'';
    }

    /* ---------- interaction: one handler set for pan + drag + resize ---------- */
    let drag=null; /* {kind:'pan'|'card'|'resize', pid, el, sx, sy, ox, oy, ow, oh, st} */
    board.addEventListener('pointerdown',e=>{
      if(e.button!==0)return;       /* right/middle click: context menu wins */
      if(drag)return;               /* one drag at a time — ignore extra touches */
      const rs=e.target.closest('.brs');
      const head=e.target.closest('.bhead');
      const card=e.target.closest('.bcard');
      if(!rs&&(e.target.closest('button')||e.target.closest('[contenteditable]')))return;
      if(rs&&card){
        const st=state.cards[card.dataset.id]||(state.cards[card.dataset.id]={});
        drag={kind:'resize',pid:e.pointerId,el:card,sx:e.clientX,sy:e.clientY,
              ow:card.offsetWidth,oh:card.offsetHeight,st};
        card.classList.add('is-drag');
      }else if(head&&card){
        const st=state.cards[card.dataset.id]||(state.cards[card.dataset.id]={});
        drag={kind:'card',pid:e.pointerId,el:card,sx:e.clientX,sy:e.clientY,
              ox:card._px||0,oy:card._py||0,st};
        card.classList.add('is-drag');
      }else if(!card){
        drag={kind:'pan',pid:e.pointerId,sx:e.clientX,sy:e.clientY,ox:cam.x,oy:cam.y};
        board.classList.add('is-pan');
      }else return;
      /* capture can race a fast release (pointer already gone) — the drag
         still works without capture, so a failure here must stay silent */
      try{board.setPointerCapture(e.pointerId);}catch(_e){}
    });
    board.addEventListener('pointermove',e=>{
      if(!drag||e.pointerId!==drag.pid)return;
      const dx=e.clientX-drag.sx,dy=e.clientY-drag.sy;
      if(drag.kind==='pan'){
        cam.x=drag.ox+dx;cam.y=drag.oy+dy;applyCam();
      }else if(drag.kind==='resize'){
        const w=Math.max(200,Math.min(960,drag.ow+dx/cam.k));
        const h=Math.max(110,Math.min(840,drag.oh+dy/cam.k));
        drag.el.style.width=w+'px';drag.el.style.height=h+'px';
        drag.el.style.maxHeight='none';  /* explicit size beats the auto cap */
        drag.st.w=Math.round(w);drag.st.h=Math.round(h);
      }else{
        const x=drag.ox+dx/cam.k,y=drag.oy+dy/cam.k;
        drag.el._px=x;drag.el._py=y;
        drag.el.style.transform=`translate(${x}px,${y}px)`;
        drag.st.x=x;drag.st.y=y;
      }
    });
    const endDrag=e=>{
      if(!drag||(e&&e.pointerId!==drag.pid))return;
      if(drag.el)drag.el.classList.remove('is-drag');
      board.classList.remove('is-pan');
      drag=null;save(state);
    };
    board.addEventListener('pointerup',endDrag);
    board.addEventListener('pointercancel',endDrag);

    board.addEventListener('wheel',e=>{
      /* wheel over a card scrolls the card's own content (dashboards are
         tall); the canvas zooms from empty board space, or Ctrl+wheel
         anywhere (the pinch-zoom convention) */
      if(e.target.closest('.bcard')&&!e.ctrlKey)return;
      e.preventDefault();
      if(drag)return;  /* zooming mid-drag would corrupt the drag's cam.k math */
      const r=board.getBoundingClientRect();
      const mx=e.clientX-r.left,my=e.clientY-r.top;
      const k2=Math.min(2,Math.max(.4,cam.k*Math.exp(-e.deltaY*.0015)));
      /* keep the point under the cursor fixed while zooming */
      cam.x=mx-(mx-cam.x)*(k2/cam.k);
      cam.y=my-(my-cam.y)*(k2/cam.k);
      cam.k=k2;applyCam();save(state);
    },{passive:false});

    /* clicks: info / collapse / remove / re-show / cluster jump */
    el.addEventListener('click',e=>{
      const info=e.target.closest('.bi');
      const min=e.target.closest('.bmin');
      const x=e.target.closest('.bx');
      const show=e.target.closest('[data-show]');
      const go=e.target.closest('[data-goto]');
      if(info){
        const card=info.closest('.bcard');
        const strip=card.querySelector('.binfo');
        const st=state.cards[card.dataset.id]||(state.cards[card.dataset.id]={});
        st.info=strip.hidden;strip.hidden=!strip.hidden;save(state);
      }else if(min){
        const card=min.closest('.bcard');
        const st=state.cards[card.dataset.id]||(state.cards[card.dataset.id]={});
        st.collapsed=!st.collapsed;card.classList.toggle('is-min',st.collapsed);save(state);
      }else if(x){
        const card=x.closest('.bcard'),id=card.dataset.id;
        if(id.startsWith('note-')){delete state.notes[id];delete state.cards[id];}
        else if(id.startsWith('embed-')){delete state.embeds[id];delete state.cards[id];}
        else{(state.cards[id]||(state.cards[id]={})).hidden=true;}
        card.remove();renderHiddenChips();save(state);
      }else if(show){
        (state.cards[show.dataset.show]||(state.cards[show.dataset.show]={})).hidden=false;
        save(state);renderCards();
      }else if(go&&ctx){
        ctx.switchTo('graph');
      }
    });

    const spawnAt=()=>({x:(80-cam.x)/cam.k,y:(120-cam.y)/cam.k});
    el.querySelector('#bd-note').addEventListener('click',()=>{
      const nid='note-'+Date.now().toString(36);
      state.notes[nid]='';
      state.cards[nid]=spawnAt();
      save(state);renderCards();
    });
    function addEmbed(src,title){
      const cleaned=safeSrc(src);
      if(!cleaned)return;
      const eid='embed-'+Date.now().toString(36);
      state.embeds[eid]={src:cleaned,title:title||cleaned};
      state.cards[eid]=spawnAt();
      save(state);renderCards();
    }
    el.querySelector('#bd-html').addEventListener('click',()=>{
      const src=prompt('Path or URL of an HTML file to load\n(paths resolve against this page, e.g. boards/hello.html):','boards/hello.html');
      if(src)addEmbed(src,src.split('/').pop());
    });
    el.querySelector('#bd-embed').addEventListener('click',()=>{
      const src=prompt('URL to embed (https://…):','https://');
      if(src&&src!=='https://')addEmbed(src);
    });
    el.querySelector('#bd-fit').addEventListener('click',fitBoard);
    el.querySelector('#bd-reset').addEventListener('click',()=>{
      /* reset positions for built-ins AND notes/embeds (anything dragged
         offscreen has no other way home); their content is kept */
      for(const d of CARD_DEFS)delete state.cards[d.id];
      for(const nid of Object.keys(state.notes))delete state.cards[nid];
      for(const eid of Object.keys(state.embeds))delete state.cards[eid];
      save(state);renderCards();
    });
    el.querySelector('#bd-refresh').addEventListener('click',async()=>{
      await fetchData();renderCards();
    });

    surface.innerHTML='<div class="ovload">reading workspace state…</div>';
    await fetchData();
    renderCards();
    fitBoard();  /* land framed on the whole board, whatever the saved camera */
  },
  show(){},hide(){}
};
