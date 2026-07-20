/* The atlas trio — Graph, Timeline, Six Degrees: three lenses over one
   dataset (space.json). They share the model, camera, selection state and
   cross-view actions inside one boot() closure, so they live in one module
   exporting three views (splitting them would force cross-imports, which the
   view contract forbids). Cross-view jumps go through ctx.switchTo (`go`).
   All graph content comes from ./data/space.json; nothing is embedded here —
   the data lives on disk, next to this page. */
import {apiFetch} from '../core/api.js';
import {toast} from '../core/ui.js';
import {renderFilesList} from '../core/fileslist.js';
import {openLeafPreview,closePreview,previewWidth} from '../core/leaf-preview.js';
import {mountCommitTimeline,resizeCommitTimeline,selectTimelineGroup,getGroupHistory,showPopoverIn,movePopoverIn,hidePopoverSoon,dtfmt as commitDtfmt} from './commit_timeline.js';
import {renderSplitTrunk} from './timeline_split_trunk.js';

let go=()=>{};   /* ctx.switchTo, captured on first mount */
const hooks={};  /* boot() assigns lifecycle hooks here once it has run */
let bootPromise=null;

/* The space maps one of three worlds: the workspace's projects (space.json,
   the artifact map), its agent sessions (sessions_graph.json, telemetry in
   the same schema), or environments (environments_graph.json — the same
   projects clustered into 5 fixed business-purpose hubs: app/ops/wiki/
   marketing/customer, instead of one hub per project). The switcher is the
   topbar's space pill (#gmode, built by app.js at startup). The choice
   persists in localStorage and switching reloads the page — boot() runs
   exactly once per load, so a reload is the sanctioned reset, and the
   registry's hash deep-link restores the active tab. */
const DATASETS={
  output:{url:'data/space.json',label:'Projects'},
  sessions:{url:'data/sessions_graph.json',label:'Sessions'},
  environments:{url:'data/environments_graph.json',label:'Environments'}
};
const MODE_KEY='space.graphDataset';
export function graphMode(){
  try{const m=localStorage.getItem(MODE_KEY);return DATASETS[m]?m:'output';}
  catch(_e){return 'output';}
}
/* Drill-down: expanding a project in the Environments space loads a
   project-scoped graph (files + sessions). The target pid rides sessionStorage
   (transient — a drill is a within-session navigation, not a persisted space);
   ensureBoot honours it only while the Environments space is active. */
const DRILL_KEY='space.drillPid';
function drillPid(){
  if(graphMode()!=='environments')return null;
  try{return sessionStorage.getItem(DRILL_KEY)||null;}catch(_e){return null;}
}
export function openDrill(pid){
  try{sessionStorage.setItem(DRILL_KEY,pid);}catch(_e){return;}
  location.reload();
}
function closeDrill(){
  try{sessionStorage.removeItem(DRILL_KEY);}catch(_e){}
  location.reload();
}
export function buildModeToggle(){
  const el=document.getElementById('gmode');
  if(!el||el.childElementCount)return;
  el.setAttribute('role','group');
  el.setAttribute('aria-label','Space');
  const mode=graphMode();
  Object.entries(DATASETS).forEach(([id,d])=>{
    const b=document.createElement('button');
    b.textContent=d.label;
    b.setAttribute('aria-pressed',String(id===mode));
    if(id===mode)b.classList.add('is-on');
    b.addEventListener('click',()=>{
      if(id===graphMode())return;
      try{localStorage.setItem(MODE_KEY,id);sessionStorage.removeItem(DRILL_KEY);}catch(_e){return;}
      /* leaving the space clears any active drill so it can't re-open later */
      location.reload();
    });
    el.appendChild(b);
  });
}

/* boot() runs exactly once, no matter which atlas lens mounts first or how
   many mount concurrently — the cached promise is the single-flight guard. */
function ensureBoot(){
  if(!bootPromise)bootPromise=(async()=>{
    const pid=drillPid();
    const url=pid?('data/project_graph.json?pid='+encodeURIComponent(pid))
      :DATASETS[graphMode()].url;
    if(!url){renderPlaceholder(DATASETS[graphMode()].label);return;}
    const res=await apiFetch(url);
    if(!res.ok){
      /* a stale/invalid drill target must not brick the space — drop it and
         fall back to the environments graph */
      if(pid){try{sessionStorage.removeItem(DRILL_KEY);}catch(_e){}location.reload();return;}
      console.warn('Space could not load '+url+':',res.error);
      throw new Error(res.error);
    }
    boot(res.data,'local file');
  })();
  return bootPromise;
}

/* Spaces without a dataset yet (environments): Files/Timeline show an honest
   placeholder instead of booting the sim with someone else's data. */
function renderPlaceholder(label){
  for(const id of ['view-graph','view-time','view-six']){
    const s=document.getElementById(id);
    if(!s||s.querySelector('.nodata'))continue;
    const box=document.createElement('div');
    box.className='nodata';
    box.innerHTML='<div class="eyebrow">'+label+'</div>'+
      '<h1>This space has no map yet.</h1>'+
      '<p>The '+label.toLowerCase()+' classifier is designed after the nav refactor — '+
      'Files and Timeline light up once it lands. The Dashboard already works.</p>';
    s.appendChild(box);
  }
  document.getElementById('view-graph')?.classList.remove('intro-dim');
}

function renderNoData(el){
  if(!el)return;
  const box=document.createElement('div');
  box.className='nodata';
  box.innerHTML='<div class="eyebrow">No data source</div>'+
    '<h1>Space reads its map from a local file.</h1>'+
    '<p>This page loads <b>'+DATASETS[graphMode()].url+'</b> from the folder it is served from, so the data stays on this machine. Serve the folder with the workspace server:</p>'+
    '<pre>cd xo-cowork-api && ./cowork-api.sh start</pre>'+
    '<p>then open <b>http://localhost:5002/space/</b></p>'+
    '<button id="nodata-retry">Retry</button>';
  el.appendChild(box);
  box.querySelector('#nodata-retry').addEventListener('click',()=>location.reload());
}

function atlasView(id,label,order,lens){
  return{
    id,label,order,
    async mount(el,ctx){
      go=ctx.switchTo;
      try{await ensureBoot();}
      catch(err){renderNoData(el);}
    },
    show(){if(hooks.setActiveView)hooks.setActiveView(lens);},
    hide(){if(hooks.setActiveView)hooks.setActiveView(null);}
  };
}
export const graphView=atlasView('graph','Files',2,'graph');
export const timeView=atlasView('time','Timeline',3,'time');
/* Six Degrees has no tab of its own: it opens from the Timeline header
   (#tsix), keeps its #/six deep link, and lights the Timeline tab. */
export const sixView={...atlasView('six','Six&nbsp;Degrees',3,'six'),hideTab:true,parentTab:'time'};

function boot(DATA,DATA_SOURCE){
/* ============================== MODEL FROM LOCAL DATA ==============================
   All graph content comes from ./data/space.json, loaded at the bottom of this file.
   Nothing is embedded here: the data lives on disk, next to this page. */
const CAT=DATA.categories;
const ACCENT='#a8d94f', ACCENT_DEEP='#83d63a';
const NODES=[];
NODES.push({id:DATA.root.id,type:'root',label:DATA.root.label,blurb:DATA.root.blurb});
DATA.hubs.forEach(h=>NODES.push({id:h.id,type:'hub',cat:h.cat,label:h.label,blurb:h.blurb,ftype:h.ftype,facts:h.facts,shape:h.shape,xotype:h.xotype}));
DATA.groups.forEach(g=>NODES.push({id:g.id,type:'group',cat:g.cat,label:g.label,blurb:g.blurb,ftype:g.ftype,facts:g.facts,shape:g.shape,xotype:g.xotype}));
DATA.leaves.forEach(l=>NODES.push({id:l.id,type:'leaf',group:l.group,shape:l.shape,tag:l.tag,label:l.label,date:l.date,blurb:l.blurb,path:l.path,ftype:l.ftype,facts:l.facts,xotype:l.xotype,clusters:l.clusters}));
const EDGES=[];
DATA.hubs.forEach(h=>EDGES.push({s:DATA.root.id,t:h.id,kind:'root',label:DATA.meta.rootEdgeLabel||'a department of XO'}));
DATA.groups.forEach(g=>EDGES.push({s:g.cat,t:g.id,kind:'hg',label:'part of'}));
DATA.leaves.forEach(l=>EDGES.push({s:l.group,t:l.id,kind:'rg',label:'part of'}));
DATA.ties.forEach(x=>EDGES.push({s:x.s,t:x.t,kind:'x',label:x.label}));

/* ============================== MODEL ============================== */
const byId=new Map(NODES.map(n=>[n.id,n]));
NODES.forEach(n=>{
  if(n.type==='leaf') n.cat=byId.get(n.group).cat;
  n.adj=[];n.x=0;n.y=0;n.vx=0;n.vy=0;n.fx=null;n.fy=null;
});
EDGES.forEach(e=>{byId.get(e.s).adj.push({e,other:e.t});byId.get(e.t).adj.push({e,other:e.s});});
NODES.forEach(n=>n.degree=n.adj.length);
const LEAVES=NODES.filter(n=>n.type==='leaf');
const GROUPS=NODES.filter(n=>n.type==='group');
const HUBS=NODES.filter(n=>n.type==='hub');
const XCOUNT=EDGES.filter(e=>e.kind==='x').length;
/* Dataset-provided copy, with the artifact map's wording as the default */
const NOUN=DATA.meta.noun||'artifacts';
document.getElementById('q').placeholder=`Search ${LEAVES.length} ${NOUN}…`;
document.getElementById('fmeta').textContent=
  `${LEAVES.length} ${NOUN} · ${GROUPS.length} clusters · ${EDGES.length} links · mapped ${DATA.meta.mappedOn} · data: ${DATA_SOURCE}`;
document.getElementById('intro-p').textContent=DATA.meta.intro||
  `Wander through ${LEAVES.length} artifacts across four departments of XO: the repos, papers, decks, and experiments that bind thirteen months of work together.`;
if(DATA.meta.introTitle)document.querySelector('#intro h1').textContent=DATA.meta.introTitle;
if(DATA.meta.introEyebrow)document.querySelector('#intro .eyebrow').textContent=DATA.meta.introEyebrow;
if(DATA.meta.timelineTitle)document.querySelector('#view-time .thead h2').textContent=DATA.meta.timelineTitle;
if(DATA.meta.timelineSub)document.getElementById('tsub').textContent=DATA.meta.timelineSub;
{ /* scrub ticks follow the dataset's own range (year shown when it changes) */
  const tk=document.querySelector('#view-time .ticks');
  const t0=+new Date(DATA.timeline.start+'T00:00:00'),t1=+new Date(DATA.timeline.end+'T00:00:00');
  if(tk&&t1>t0){
    let py=null;
    tk.innerHTML=[0,.25,.5,.75,1].map((f,i)=>{
      const d=new Date(t0+(t1-t0)*f);
      const mon=d.toLocaleDateString('en-US',{month:'short'}).toUpperCase();
      const lab=(py===d.getFullYear()&&i<4)?mon:`${mon} ${d.getFullYear()}`;
      py=d.getFullYear();
      return `<span>${lab}</span>`;
    }).join('');
  }
}

const colorOf=n=>n.type==='root'?'#e9e4d9':CAT[n.cat].color;
function radiusOf(n){
  if(n.type==='root')return 17;
  if(n.type==='hub')return 13;
  if(n.type==='group')return 5.5+Math.min(5,n.adj.length*.22);
  return 3.3+Math.min(4.2,(n.degree-1)*.85);
}
NODES.forEach(n=>n.r=radiusOf(n));
const fmtDate=d=>new Date(d+'T00:00:00').toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric'});
const fmtMY=t=>new Date(t).toLocaleDateString('en-US',{year:'numeric',month:'short'});
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const hexA=(h,a)=>`rgba(${parseInt(h.slice(1,3),16)},${parseInt(h.slice(3,5),16)},${parseInt(h.slice(5,7),16)},${a})`;
const REDUCED=matchMedia('(prefers-reduced-motion: reduce)').matches;

/* expansion + filter state */
const expanded=new Map(GROUPS.map(g=>[g.id,true]));
let deptFilter=null;
const isShown=n=>{
  if(n.type==='leaf'){
    if(!expanded.get(n.group))return false;
    if(deptFilter&&n.cat!==deptFilter)return false;
    return true;
  }
  return true;
};
const dimByFilter=n=>deptFilter&&n.cat&&n.cat!==deptFilter;

/* XO data-type overlay: every node carries one of four tags (output/inbox/
   session/system, from DATA.meta.typeLegend). Lightweight ('dim') types
   render faded + smaller until their chip is selected; selecting any chip
   spotlights that type and fades the rest. Filtering dims, never hides —
   the graph's structure stays put. */
const TYPE_DEFS=DATA.meta.typeLegend||[];
const TYPE_BY_ID=Object.fromEntries(TYPE_DEFS.map(t=>[t.id,t]));
/* Weight-dimming only makes sense in a MIXED graph: a space whose every
   node is one type (Sessions: all 'session') must not render uniformly
   faded. */
const TYPE_MIXED=new Set(NODES.map(n=>n.xotype).filter(Boolean)).size>1;
let typeFilter=null;
function typeAlpha(n){
  if(!n.xotype||!TYPE_DEFS.length||!TYPE_MIXED)return 1;
  if(typeFilter)return n.xotype===typeFilter?1:.22;
  return TYPE_BY_ID[n.xotype]?.weight==='dim'?.45:1;
}
const typeShrunk=n=>TYPE_MIXED&&!typeFilter&&n.xotype&&TYPE_BY_ID[n.xotype]?.weight==='dim';
const shownNodes=()=>NODES.filter(isShown);
const shownEdges=()=>EDGES.filter(e=>isShown(byId.get(e.s))&&isShown(byId.get(e.t)));

/* layout seed */
const HUB_ANGLE=DATA.hubAngles;
const HUB_R=520;
/* root id comes from the data — never hardcode it ('xo' today, anything
   tomorrow); byId.get(unknown).fx throws and kills boot. */
const root=byId.get(DATA.root.id);root.fx=0;root.fy=0;
document.getElementById('root-name').textContent=DATA.root.label;
document.getElementById('root-reset').textContent='Reset to '+DATA.root.label;
HUBS.forEach(h=>{h.ax=Math.cos(HUB_ANGLE[h.cat])*HUB_R;h.ay=Math.sin(HUB_ANGLE[h.cat])*HUB_R;h.x=h.ax;h.y=h.ay;});
/* Each project owns an equal sector of the circle; its cluster fan must stay
   inside it. A fixed .5 rad step wraps the whole circle once a project has
   ~13+ clusters (generated data easily does), seeding clusters in other
   projects' territory — and the hub spring constrains distance, not angle,
   so they never migrate home. Radius staggers to relieve arc crowding. */
const SECTOR=Math.PI*2/Math.max(1,Object.keys(HUB_ANGLE).length);
GROUPS.forEach(g=>{
  const sib=GROUPS.filter(x=>x.cat===g.cat),k=sib.indexOf(g),m=sib.length;
  const step=Math.min(.5,SECTOR*.85/Math.max(1,m));
  const a=HUB_ANGLE[g.cat]+(k-(m-1)/2)*step;
  const r=HUB_R+170+(k%3)*70;
  g.x=Math.cos(a)*r;g.y=Math.sin(a)*r;
});
LEAVES.forEach((l,i)=>{
  const g=byId.get(l.group);
  const a=(i*.618033*Math.PI*2)%(Math.PI*2);
  l.x=g.x+Math.cos(a)*(30+ (i%5)*11);
  l.y=g.y+Math.sin(a)*(30+ (i%5)*11);
});

/* ============================== SIMULATION ============================== */
let simAlpha=1;
let rootId=DATA.root.id,rootDepths=null;
/* Tie springs are weak by default (cross-references must not distort the
   tree layout); a dataset can strengthen them via meta.tieSpring — the
   Environments space does, so a project tied to several clusters settles
   at their midpoint. */
const SPR={root:{d:HUB_R,k:.02},hg:{d:175,k:.05},rg:{d:62,k:.08},
  x:DATA.meta.tieSpring||{d:210,k:.005}};
const CHG={root:-3400,hub:-2600,group:-1000,leaf:-235};
function simTick(){
  const vs=shownNodes(),es=shownEdges();
  for(let i=0;i<vs.length;i++){
    const a=vs[i],qa=CHG[a.type]||CHG.leaf;
    for(let j=i+1;j<vs.length;j++){
      const b=vs[j];
      let dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy;
      if(d2<1){dx=Math.random()-.5;dy=Math.random()-.5;d2=1;}
      if(d2>320*320)continue;
      const d=Math.sqrt(d2),qb=CHG[b.type]||CHG.leaf;
      let f=Math.min(qa,qb)/d2*simAlpha;
      const rr=a.r+b.r+7;
      if(d<rr)f-=(rr-d)*.3;
      const fx=dx/d*f,fy=dy/d*f;
      if(a.fx==null){a.vx+=fx;a.vy+=fy;}
      if(b.fx==null){b.vx-=fx;b.vy-=fy;}
    }
  }
  for(const e of es){
    const a=byId.get(e.s),b=byId.get(e.t),sp=SPR[e.kind];
    let dx=b.x-a.x,dy=b.y-a.y;
    const d=Math.max(1,Math.hypot(dx,dy)),f=(d-sp.d)*sp.k*simAlpha;
    const fx=dx/d*f,fy=dy/d*f;
    if(a.fx==null){a.vx+=fx;a.vy+=fy;}
    if(b.fx==null){b.vx-=fx;b.vy-=fy;}
  }
  const R0=byId.get(rootId);
  for(const n of vs){
    if(rootId===DATA.root.id&&n.type==='hub'){n.vx+=(n.ax-n.x)*.05*simAlpha;n.vy+=(n.ay-n.y)*.05*simAlpha;}
    else if(rootDepths&&n.id!==rootId&&n.fx==null){
      /* concentric neighbourhood rings around the chosen root */
      const d=rootDepths.get(n.id)??6;
      let dx=n.x-R0.x,dy=n.y-R0.y;
      let dist=Math.hypot(dx,dy);
      if(dist<1){dx=Math.random()-.5;dy=Math.random()-.5;dist=1;}
      const f=(d*110-dist)*.045*simAlpha;
      n.vx+=dx/dist*f;n.vy+=dy/dist*f;
    }
    else if(n.fx==null){n.vx-=(n.x-R0.x)*.001*simAlpha;n.vy-=(n.y-R0.y)*.001*simAlpha;}
    if(n.fx!=null){n.x=n.fx;n.y=n.fy;n.vx=0;n.vy=0;continue;}
    n.vx*=.7;n.vy*=.7;
    /* Speed limit: with generated data a group can own 100+ leaves, whose
       summed spring stiffness makes explicit Euler diverge (positions hit
       1e20 and the camera fit goes with them). Clamping per-tick velocity
       bounds the integrator regardless of cluster size. */
    const _sp=Math.hypot(n.vx,n.vy);
    if(_sp>60){n.vx*=60/_sp;n.vy*=60/_sp;}
    n.x+=n.vx;n.y+=n.vy;
  }
  /* Decay to a full stop instead of idling at .02 forever — a perpetual 2%
     simmer keeps every force (incl. the centering bias) acting for eternity,
     so the layout jiggles when zoomed and whole projects drift toward the
     root. Interactions reheat() as before. */
  if(simAlpha>.003)simAlpha*=.9885;else simAlpha=0;
}
const reheat=a=>{simAlpha=Math.max(simAlpha,a);};

/* ============================== CAMERA ============================== */
const cam={x:0,y:0,k:.7};
let camAnim=null;
const easeCubicInOut=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;
function flyTo(x,y,k,ms=820){
  if(REDUCED)ms=1;
  camAnim={t0:performance.now(),ms,from:{...cam},to:{x,y,k}};
}
function stepCam(now){
  if(!camAnim)return;
  const t=Math.min(1,(now-camAnim.t0)/camAnim.ms),e=easeCubicInOut(t);
  cam.x=camAnim.from.x+(camAnim.to.x-camAnim.from.x)*e;
  cam.y=camAnim.from.y+(camAnim.to.y-camAnim.from.y)*e;
  cam.k=camAnim.from.k+(camAnim.to.k-camAnim.from.k)*e;
  if(t>=1)camAnim=null;
}
function fitNodes(ids,pad=120,kmax=2.2){
  const ns=ids.map(id=>byId.get(id));
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  ns.forEach(n=>{x0=Math.min(x0,n.x);y0=Math.min(y0,n.y);x1=Math.max(x1,n.x);y1=Math.max(y1,n.y);});
  const k=Math.max(.25,Math.min(kmax,.9*Math.min(GW/(x1-x0+pad),GH/(y1-y0+pad))));
  flyTo((x0+x1)/2,(y0+y1)/2,k,900);
}

/* ============================== GRAPH RENDER ============================== */
const gcv=document.getElementById('gcanvas'),gc=gcv.getContext('2d');
let GW=0,GH=0,dpr=1;
let hoverId=null,selId=null,focusSet=null,focusDepth=0;
let pathIds=null,pathEdges=null,pathReveal=0;
function neighborhood(id,depth){
  const set=new Set([id]);
  let frontier=[id];
  for(let d=0;d<depth;d++){
    const next=[];
    for(const u of frontier)for(const {other} of byId.get(u).adj){
      if(!set.has(other)&&isShown(byId.get(other))){set.add(other);next.push(other);}
    }
    frontier=next;
  }
  return set;
}
function drawShape(c,x,y,r,shape){
  c.beginPath();
  if(shape==='diamond'){const s=r*1.25;c.moveTo(x,y-s);c.lineTo(x+s,y);c.lineTo(x,y+s);c.lineTo(x-s,y);c.closePath();}
  else if(shape==='slab'){const w=r*1.55,h=r*.95;c.rect(x-w,y-h,w*2,h*2);}         /* slides: wide 16:10 card */
  else if(shape==='stack'){const s=r*.92,o=r*.38;                                  /* docs: two offset pages */
    c.rect(x-s-o,y-s+o,s*2,s*2);c.rect(x-s+o,y-s-o,s*2,s*2);}
  else c.arc(x,y,r,0,Math.PI*2);
}
/* ---- enclosed clusters (meta.enclose): a soft convex hull drawn around
   each cluster's members — its group node plus every leaf whose primary
   group OR secondary membership (leaf.clusters) is this cluster. Shared
   projects therefore sit inside every hull they belong to, at the
   midpoint the tie springs pull them to. ---- */
function _convexHull(pts){
  if(pts.length<3)return pts.slice();
  const p=pts.slice().sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
  const cross=(o,a,b)=>(a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]);
  const lo=[],hi=[];
  for(const pt of p){
    while(lo.length>1&&cross(lo[lo.length-2],lo[lo.length-1],pt)<=0)lo.pop();
    lo.push(pt);
  }
  for(let i=p.length-1;i>=0;i--){
    const pt=p[i];
    while(hi.length>1&&cross(hi[hi.length-2],hi[hi.length-1],pt)<=0)hi.pop();
    hi.push(pt);
  }
  lo.pop();hi.pop();
  return lo.concat(hi);
}
let _encloseCaps=[];  /* per-hull caption anchors, drawn in the screen pass */
const _hubByCat={};HUBS.forEach(h=>{_hubByCat[h.cat]=h;});
function drawEnclosures(k){
  const PAD=42;
  _encloseCaps=[];
  for(const g of GROUPS){
    const pts=[[g.x,g.y]];
    for(const l of LEAVES){
      if(!isShown(l))continue;
      if(l.group===g.id||(l.clusters&&l.clusters.length>1&&l.clusters.includes(g.cat)))
        pts.push([l.x,l.y]);
    }
    if(pts.length<2)continue;
    const col=CAT[g.cat]?.color||'#888';
    let cx=0,cy=0;
    for(const [x,y] of pts){cx+=x;cy+=y;}
    cx/=pts.length;cy/=pts.length;
    let minY=Infinity;
    gc.beginPath();
    if(pts.length===2){
      /* two points: a padded capsule reads better than a degenerate hull */
      const r=Math.hypot(pts[0][0]-pts[1][0],pts[0][1]-pts[1][1])/2+PAD;
      gc.arc(cx,cy,r,0,Math.PI*2);
      minY=cy-r;
    }else{
      const hull=_convexHull(pts).map(([x,y])=>{
        const dx=x-cx,dy=y-cy,d=Math.hypot(dx,dy)||1;
        return [x+dx/d*PAD,y+dy/d*PAD];
      });
      const mid=(a,b)=>[(a[0]+b[0])/2,(a[1]+b[1])/2];
      let m=mid(hull[hull.length-1],hull[0]);
      gc.moveTo(m[0],m[1]);
      for(let i=0;i<hull.length;i++){
        const nm=mid(hull[i],hull[(i+1)%hull.length]);
        gc.quadraticCurveTo(hull[i][0],hull[i][1],nm[0],nm[1]);
        if(hull[i][1]<minY)minY=hull[i][1];
      }
      gc.closePath();
    }
    gc.fillStyle=hexA(col,.055);gc.fill();
    gc.strokeStyle=hexA(col,.32);gc.lineWidth=1.2/Math.sqrt(k);
    gc.setLineDash([5/k,4/k]);gc.stroke();gc.setLineDash([]);
    if(g.desc)_encloseCaps.push({label:g.label,desc:g.desc,col,cx,topY:minY});
  }
}

function drawGraph(now){
  gc.setTransform(dpr,0,0,dpr,0,0);
  gc.clearRect(0,0,GW,GH);
  /* ambient tints */
  let grd=gc.createRadialGradient(GW*.74,GH*.32,0,GW*.74,GH*.32,GW*.5);
  grd.addColorStop(0,'rgba(168,217,79,.05)');grd.addColorStop(1,'rgba(0,0,0,0)');
  gc.fillStyle=grd;gc.fillRect(0,0,GW,GH);
  grd=gc.createRadialGradient(GW*.2,GH*.8,0,GW*.2,GH*.8,GW*.45);
  grd.addColorStop(0,'rgba(111,147,173,.04)');grd.addColorStop(1,'rgba(0,0,0,0)');
  gc.fillStyle=grd;gc.fillRect(0,0,GW,GH);

  stepCam(now);
  const k=cam.k;
  gc.setTransform(dpr*k,0,0,dpr*k,dpr*(GW/2-cam.x*k),dpr*(GH/2-cam.y*k));
  const es=shownEdges(),vs=shownNodes();
  if(DATA.meta.enclose)drawEnclosures(k);
  const inFocus=id=>!focusSet||focusSet.has(id);
  /* path reveal progress */
  let revealSeg=1e9;
  if(pathIds){
    const per=REDUCED?0:420;
    revealSeg=per?Math.min(pathIds.length,(now-pathReveal)/per):1e9;
  }
  /* ---- edges ---- */
  for(const e of es){
    /* enclose mode: the root→hub→group scaffolding is invisible (the hull
       IS the cluster); those edges would just dangle to hidden anchors */
    if(DATA.meta.enclose&&(e.kind==='root'||e.kind==='hg'))continue;
    const a=byId.get(e.s),b=byId.get(e.t);
    let alpha,width,color;
    if(pathIds){
      const idx=pathEdges?pathEdges.indexOf(e):-1;
      if(idx>=0&&idx<revealSeg){alpha=.85;width=2/k;color=ACCENT;}
      else{alpha=.015;width=.7/k;color='#cfc9bb';}
    }else if(focusSet){
      const lit=(e.s===selId||e.t===selId)&&inFocus(e.s)&&inFocus(e.t);
      const semi=inFocus(e.s)&&inFocus(e.t);
      if(lit){alpha=.42;width=1.4/k;color=ACCENT;}
      else if(semi){alpha=.14;width=.8/k;color='#cfc9bb';}
      else{alpha=.012;width=.7/k;color='#78746c';}
    }else{
      const fdim=dimByFilter(a)||dimByFilter(b);
      alpha=(e.kind==='x'?.10:e.kind==='root'?.07:.05)*(fdim?.25:1);
      width=(e.kind==='x'?.9:.7)/k;color=e.kind==='x'?'#cfc9bb':'#b4afa4';
    }
    gc.beginPath();
    if(e.kind==='x'){
      const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1;
      gc.moveTo(a.x,a.y);
      gc.quadraticCurveTo((a.x+b.x)/2-dy/d*d*.13,(a.y+b.y)/2+dx/d*d*.13,b.x,b.y);
    }else{gc.moveTo(a.x,a.y);gc.lineTo(b.x,b.y);}
    gc.strokeStyle=hexA(color,alpha);gc.lineWidth=width;gc.lineCap='round';gc.stroke();
  }
  /* ---- nodes ---- */
  const drawOrder=pathIds?[...vs].sort((a,b)=>(pathIds.includes(a.id)?1:0)-(pathIds.includes(b.id)?1:0)):vs;
  for(const n of drawOrder){
    /* enclose mode: hubs and root are invisible sim anchors — the hull and
       its caption carry the cluster's identity */
    if(DATA.meta.enclose&&(n.type==='hub'||n.type==='root'))continue;
    const col=colorOf(n);
    let a=1;
    if(pathIds)a=pathIds.includes(n.id)?1:.10;
    else if(focusSet)a=focusSet.has(n.id)?1:.14;
    else if(dimByFilter(n))a=.18;
    else if(n.type!=='root'&&n.type!=='hub')a=typeAlpha(n);  /* XO data-type weight */
    gc.globalAlpha=a;
    if(n.type==='root'){
      /* the actual XO mark: white X chevrons, lime O chevrons */
      gc.lineWidth=2.4/Math.sqrt(k);gc.lineJoin='miter';gc.lineCap='butt';
      const sc=.075;
      const CHEV=[
        ['#e9e4d9',[[37,166],[118,247],[31,335]]],
        ['#e9e4d9',[[245,166],[163,247],[251,335]]],
        [ACCENT_DEEP,[[328,165],[247,247],[334,334]]],
        [ACCENT_DEEP,[[381,165],[462,247],[375,334]]],
      ];
      for(const[col,pts]of CHEV){
        gc.strokeStyle=col;gc.beginPath();
        pts.forEach(([px,py],i)=>{
          const wx=n.x+(px-246.5)*sc,wy=n.y+(py-250)*sc;
          i?gc.lineTo(wx,wy):gc.moveTo(wx,wy);
        });
        gc.stroke();
      }
    }else if(n.type==='hub'){
      gc.beginPath();gc.arc(n.x,n.y,n.r,0,Math.PI*2);
      gc.fillStyle=hexA(col,.13);gc.fill();
      gc.strokeStyle=hexA(col,.9);gc.lineWidth=1.4/Math.sqrt(k);gc.stroke();
      drawShape(gc,n.x,n.y,2.6,n.shape);gc.fillStyle=col;gc.fill();
    }else if(n.type==='group'){
      drawShape(gc,n.x,n.y,n.r,n.shape);
      gc.fillStyle=hexA(col,.22);gc.fill();
      gc.strokeStyle=hexA(col,.8);gc.lineWidth=1.1/Math.sqrt(k);gc.stroke();
      if(!expanded.get(n.id)){
        gc.beginPath();gc.arc(n.x,n.y,n.r+3.2,0,Math.PI*2);
        gc.setLineDash([2.4/k,3.2/k]);
        gc.strokeStyle=hexA(col,.4);gc.lineWidth=.9/Math.sqrt(k);gc.stroke();
        gc.setLineDash([]);
      }
    }else{
      const hl=n.id===hoverId||n.id===selId||(pathIds&&pathIds.includes(n.id));
      const r=n.r*(hl?1.5:typeShrunk(n)?.85:1);
      drawShape(gc,n.x,n.y,r,n.shape);
      if(n.shape==='ring'){
        gc.strokeStyle=col;gc.lineWidth=1.5/Math.sqrt(k);gc.stroke();
      }else{gc.fillStyle=col;gc.fill();}
      if(n.id===selId||(pathIds&&pathIds.includes(n.id))){
        drawShape(gc,n.x,n.y,r+3.4/Math.sqrt(k),n.shape);
        gc.strokeStyle=hexA(ACCENT,.8);gc.lineWidth=1.4/Math.sqrt(k);gc.stroke();
      }else if(hl){
        drawShape(gc,n.x,n.y,r+3/Math.sqrt(k),n.shape);
        gc.strokeStyle='rgba(233,228,217,.9)';gc.lineWidth=1.2/Math.sqrt(k);gc.stroke();
      }
    }
    if(n.id===rootId&&n.type!=='root'){
      gc.beginPath();gc.arc(n.x,n.y,n.r+7/Math.sqrt(k),0,Math.PI*2);
      gc.strokeStyle=hexA(ACCENT,.65);gc.lineWidth=1.4/Math.sqrt(k);gc.stroke();
      gc.beginPath();gc.arc(n.x,n.y,n.r+11/Math.sqrt(k),0,Math.PI*2);
      gc.strokeStyle=hexA(ACCENT,.2);gc.lineWidth=1/Math.sqrt(k);gc.stroke();
    }
    gc.globalAlpha=1;
  }
  /* ---- labels (screen space) ---- */
  gc.setTransform(dpr,0,0,dpr,0,0);
  gc.textAlign='center';
  /* enclosure captions: the cluster's name + one-line explanation, above
     each hull. Screen-space so they stay legible at any zoom. */
  if(DATA.meta.enclose&&!pathIds&&!focusSet){
    for(const c of _encloseCaps){
      const sx=(c.cx-cam.x)*k+GW/2,sy=(c.topY-cam.y)*k+GH/2;
      if(sx<-260||sx>GW+260||sy<-40||sy>GH+90)continue;
      gc.font='600 15px '+SERIF;
      halo(c.label,sx,sy-26,hexA(c.col,.98));
      gc.font='400 10.5px '+SANS;
      halo(c.desc,sx,sy-11,'rgba(201,195,181,.78)');
    }
  }
  for(const n of vs){
    let a=1;
    if(pathIds)a=pathIds.includes(n.id)?1:0;
    else if(focusSet)a=focusSet.has(n.id)?1:0;
    else if(dimByFilter(n))a=.25;
    if(a===0)continue;
    const sx=(n.x-cam.x)*k+GW/2,sy=(n.y-cam.y)*k+GH/2;
    if(sx<-100||sx>GW+100||sy<-50||sy>GH+50)continue;
    if(n.type==='hub'){
      /* enclose mode: the hull caption is the cluster's identity — skip the
         hub's own name to avoid a duplicate label */
      if(DATA.meta.enclose)continue;
      gc.font='500 17px '+SERIF;
      halo(n.label,sx,sy-n.r*k-12,`rgba(233,228,217,${.94*a})`);
      gc.font='400 8.5px '+MONO;
      halo(`${LEAVES.filter(l=>l.cat===n.cat).length} ${NOUN.toUpperCase()}`,sx,sy+n.r*k+16,`rgba(125,120,109,${a})`,.14);
    }else if(n.type==='group'){
      const on=n.id===hoverId||n.id===selId||(focusSet&&focusSet.has(n.id));
      if(!(on||k>.8))continue;
      const closed=!expanded.get(n.id);
      gc.font='400 9px '+MONO;
      const t=n.label.toUpperCase()+(closed?` +${LEAVES.filter(l=>l.group===n.id).length}`:'');
      halo(t,sx,sy-n.r*k-7,`rgba(179,173,160,${.72*a})`,.1);
    }else if(n.type==='leaf'){
      const on=n.id===hoverId||n.id===selId||n.id===rootId||(focusSet&&focusSet.has(n.id))||(pathIds&&pathIds.includes(n.id));
      if(!(on||k>1.55||(k>1.05&&n.degree>=4)))continue;
      gc.font='400 11px '+SANS;
      halo(n.label,sx,sy-n.r*k-7,on?`rgba(233,228,217,${.94*a})`:`rgba(179,173,160,${.62*a})`);
    }
  }
  gc.globalAlpha=1;
  /* pulse ring */
  if(pulseN){
    const t=(now-pulseN.t0)/1100;
    if(t>1)pulseN=null;
    else{
      const n=byId.get(pulseN.id);
      const sx=(n.x-cam.x)*k+GW/2,sy=(n.y-cam.y)*k+GH/2;
      gc.beginPath();gc.arc(sx,sy,n.r*k+t*44,0,Math.PI*2);
      gc.strokeStyle=hexA(ACCENT,.7*(1-t));gc.lineWidth=1.8;gc.stroke();
    }
  }
  /* settling status */
  document.getElementById('simstat').style.opacity=simAlpha>.05?1:0;
}
const SERIF=`"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif`;
const SANS=`system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif`;
const MONO=`ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace`;
function halo(s,x,y,fill,tracking){
  if(tracking){gc.save();/* cheap letterspacing for tiny mono caps */
    gc.letterSpacing=(tracking*10)+'px';}
  gc.lineWidth=3.5;gc.strokeStyle='rgba(11,12,15,.88)';gc.lineJoin='round';
  gc.strokeText(s,x,y);gc.fillStyle=fill;gc.fillText(s,x,y);
  if(tracking)gc.restore();
}
let pulseN=null;

/* ============================== GRAPH INTERACTION ============================== */
let drag=null,pan=false,downX=0,downY=0,moved=false,lastX=0,lastY=0;
const toWorld=(mx,my)=>({x:(mx-GW/2)/cam.k+cam.x,y:(my-GH/2)/cam.k+cam.y});
/* Pointer events give viewport coordinates, but drawing/toWorld are in
   canvas-local space and the canvas sits below the header — feed clientX/Y
   in directly and every hit test lands one header-height off. */
const evXY=e=>{const r=gcv.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top];};
function pick(mx,my){
  const w=toWorld(mx,my);
  let best=null,bd=1e9;
  for(const n of shownNodes()){
    const d=Math.hypot(n.x-w.x,n.y-w.y);
    const hit=Math.max(n.r+4/cam.k,12/cam.k);
    if(d<hit&&d<bd){bd=d;best=n;}
  }
  return best;
}
gcv.addEventListener('pointerdown',e=>{
  gcv.setPointerCapture(e.pointerId);
  dismissIntro();
  downX=lastX=e.clientX;downY=lastY=e.clientY;moved=false;
  const n=pick(...evXY(e));
  if(n&&n.type!=='root'){drag=n;n.fx=n.x;n.fy=n.y;}
  else pan=true;
  camAnim=null;
});
gcv.addEventListener('pointermove',e=>{
  if(drag){
    if(Math.hypot(e.clientX-downX,e.clientY-downY)>4)moved=true;
    const w=toWorld(...evXY(e));
    drag.fx=w.x;drag.fy=w.y;reheat(.3);
    hideHC();
  }else if(pan){
    if(Math.hypot(e.clientX-downX,e.clientY-downY)>4)moved=true;
    cam.x-=(e.clientX-lastX)/cam.k;cam.y-=(e.clientY-lastY)/cam.k;
    lastX=e.clientX;lastY=e.clientY;
    hideHC();
  }else{
    const n=pick(...evXY(e));
    hoverId=n?n.id:null;
    gcv.style.cursor=n?'pointer':'default';
    if(n)showHC(n,e.clientX,e.clientY);else hideHC();
  }
});
let lastUp=0,clickT=null;
gcv.addEventListener('pointerup',e=>{
  if(drag){
    const d=drag;drag=null;
    if(d.type!=='root'&&d.id!==rootId){d.fx=null;d.fy=null;}
    /* the current root stays pinned where it was dropped */
  }
  pan=false;
  if(moved)return;
  const n=pick(...evXY(e));
  const now=performance.now();
  if(now-lastUp<300){
    clearTimeout(clickT);clickT=null;lastUp=0;
    onDbl(n);return;
  }
  lastUp=now;
  clickT=setTimeout(()=>{clickT=null;onClick(n);},260);
});
function onClick(n){
  if(!n){clearFocus();clearPath();return;}
  clearPath();
  select(n.id,1);
}
function onDbl(n){
  if(!n)return;
  if(n.type==='group'){toggleGroup(n);return;}
  if(n.type==='hub'){
    const gs=GROUPS.filter(g=>g.cat===n.cat);
    const anyClosed=gs.some(g=>!expanded.get(g.id));
    gs.forEach(g=>setExp(g,anyClosed));reheat(.5);
    toast(anyClosed?`${CAT[n.cat].name} opened`:`${CAT[n.cat].name} collapsed`);
    return;
  }
  if(selId===n.id&&focusDepth===1){select(n.id,2);toast('Expanded to two degrees');}
  else select(n.id,2);
}
const PANEL_W=352;
/* When the leaf preview is open it sits left of the detail panel; keep the
   selected node visible in the remaining graph viewport. */
function focusPanOffset(kT){
  if(GW<=760)return 0;
  return (PANEL_W+previewWidth())/2/kT;
}
function select(id,depth,fly=true){
  selId=id;focusDepth=depth;
  focusSet=neighborhood(id,depth);
  const n=byId.get(id);
  document.getElementById('crumb-name').textContent=n.label;
  document.getElementById('crumb-depth').textContent=`${depth} hop${depth>1?'s':''} · ${focusSet.size} nodes`;
  document.getElementById('crumb').classList.add('is-on');
  openPanel(n);
  if(fly){
    const kT=Math.max(cam.k,1.6);
    flyTo(n.x+focusPanOffset(kT),n.y,kT);
  }
}
function clearFocus(){
  selId=null;focusSet=null;focusDepth=0;
  document.getElementById('crumb').classList.remove('is-on');
  closePanel();
}
function clearPath(){pathIds=null;pathEdges=null;}
function setExp(g,v){
  if(expanded.get(g.id)===v)return;
  expanded.set(g.id,v);
  if(v){
    const kids=LEAVES.filter(l=>l.group===g.id);
    kids.forEach((l,i)=>{
      const a=i/kids.length*Math.PI*2;
      l.x=g.x+Math.cos(a)*(18+(i%4)*9);l.y=g.y+Math.sin(a)*(18+(i%4)*9);
      l.vx=0;l.vy=0;
    });
  }
}
function toggleGroup(g){
  setExp(g,!expanded.get(g.id));reheat(.5);
  if(selId&&!isShown(byId.get(selId)))clearFocus();
  if(focusSet&&selId)focusSet=neighborhood(selId,focusDepth);
}
gcv.addEventListener('wheel',e=>{
  e.preventDefault();dismissIntro();camAnim=null;
  const f=Math.exp(-e.deltaY*.0016);
  const nk=Math.max(.22,Math.min(5,cam.k*f));
  const [mx,my]=evXY(e);
  const w=toWorld(mx,my);
  cam.x=w.x-(mx-GW/2)/nk;
  cam.y=w.y-(my-GH/2)/nk;
  cam.k=nk;
},{passive:false});
document.getElementById('crumb-clear').addEventListener('click',()=>{clearFocus();clearPath();});
document.getElementById('intro-cta').addEventListener('click',dismissIntro);
let introGone=false;
function dismissIntro(){
  if(introGone)return;introGone=true;
  document.getElementById('intro').classList.add('is-gone');
  document.getElementById('view-graph').classList.remove('intro-dim');
}

/* ============================== RE-ROOT ============================== */
const rootdd=document.getElementById('rootdd');
function computeDepths(rid){
  const m=new Map([[rid,0]]);
  let fr=[rid];
  while(fr.length){
    const nx=[];
    for(const u of fr)for(const{other}of byId.get(u).adj){
      if(!m.has(other)){m.set(other,m.get(u)+1);nx.push(other);}
    }
    fr=nx;
  }
  return m;
}
function setRoot(id){
  if(rootId===id){closeRootDD();return;}
  const old=byId.get(rootId);
  old.fx=null;old.fy=null;
  rootId=id;
  const r=byId.get(id);
  ensureShown(r);
  if(id===DATA.root.id){
    r.fx=0;r.fy=0;rootDepths=null;
  }else{
    r.fx=r.x;r.fy=r.y;rootDepths=computeDepths(id);
  }
  clearFocus();clearPath();
  document.getElementById('root-name').textContent=r.label;
  reheat(.8);
  go('graph');
  flyTo(r.fx,r.fy,Math.min(Math.max(cam.k,.55),.9),900);
  toast(id===DATA.root.id?'Back to the full space':'Rooted on '+r.label);
  closeRootDD();
}
function closeRootDD(){rootdd.classList.remove('is-open');}
document.getElementById('root-btn').addEventListener('click',e=>{
  e.stopPropagation();
  rootdd.classList.toggle('is-open');
  if(rootdd.classList.contains('is-open')){
    const q=document.getElementById('root-q');
    q.value='';q.focus();
  }
});
document.getElementById('root-reset').addEventListener('click',()=>setRoot(DATA.root.id));
rootdd.addEventListener('click',e=>e.stopPropagation());
addEventListener('click',e=>{
  if(!rootdd.classList.contains('is-open'))return;
  if(!e.target.closest('.rootpick'))closeRootDD();
});

/* dept chips */
const chipsEl=document.getElementById('chips');
const chipDefs=[{id:null,label:'All'},...Object.entries(CAT).map(([id,c])=>({id,label:c.name}))];
chipDefs.forEach(d=>{
  const b=document.createElement('button');
  b.textContent=d.label;
  if(d.id===null)b.classList.add('is-on');
  b.addEventListener('click',()=>{
    deptFilter=d.id;
    [...chipsEl.children].forEach(x=>x.classList.remove('is-on'));
    b.classList.add('is-on');
    if(selId&&!isShown(byId.get(selId))){clearFocus();}
    if(focusSet&&selId)focusSet=neighborhood(selId,focusDepth);
    reheat(.4);
  });
  chipsEl.appendChild(b);
});

/* XO data-type chips — spotlight one of the four types (dims, never hides) */
{
  const tEl=document.getElementById('typechips');
  if(tEl&&TYPE_DEFS.length&&TYPE_MIXED){
    const counts={};
    LEAVES.forEach(l=>{if(l.xotype)counts[l.xotype]=(counts[l.xotype]||0)+1;});
    const defs=[{id:null,label:'All'},...TYPE_DEFS.filter(t=>counts[t.id])
      .map(t=>({id:t.id,label:`${t.label} ${counts[t.id]}`}))];
    defs.forEach(d=>{
      const b=document.createElement('button');
      b.textContent=d.label;
      if(d.id===null)b.classList.add('is-on');
      b.addEventListener('click',()=>{
        typeFilter=d.id;
        [...tEl.children].forEach(x=>x.classList.remove('is-on'));
        b.classList.add('is-on');
        reheat(.25);
      });
      tEl.appendChild(b);
    });
  }
}
/* legend + counts */
{
  const lg=document.getElementById('legend');
  const GLYPH={
    disc:'<svg width="10" height="10"><circle cx="5" cy="5" r="3.6" fill="#b3ada0"/></svg>',
    ring:'<svg width="10" height="10"><circle cx="5" cy="5" r="3.1" fill="none" stroke="#b3ada0" stroke-width="1.4"/></svg>',
    diamond:'<svg width="10" height="10"><rect x="5" y="0.9" width="5.8" height="5.8" fill="#b3ada0" transform="rotate(45 5 5)"/></svg>',
    stack:'<svg width="11" height="10"><rect x="1" y="3" width="6" height="6" fill="none" stroke="#b3ada0" stroke-width="1"/><rect x="4" y="1" width="6" height="6" fill="#b3ada0"/></svg>',
    slab:'<svg width="12" height="10"><rect x="0.5" y="2.7" width="11" height="4.6" fill="#b3ada0"/></svg>'
  };
  const shapeDefs=DATA.meta.shapeLegend||
    [{shape:'disc',label:'code'},{shape:'ring',label:'document'},{shape:'diamond',label:'experiment'}];
  lg.innerHTML=Object.values(CAT).map(c=>`<span class="li"><span class="sw" style="background:${c.color}"></span>${c.name}</span>`).join('')+
    shapeDefs.map((d,i)=>`<span class="li"${i===0?' style="margin-left:6px"':''}>${GLYPH[d.shape]||GLYPH.disc}${esc(d.label)}</span>`).join('')+
    TYPE_DEFS.map((t,i)=>`<span class="li${t.weight==='dim'?' li-dim':''}"${i===0?' style="margin-left:6px"':''}><span class="sw sw-ring"></span>${esc(t.label.toLowerCase())}</span>`).join('');
  document.getElementById('counts').textContent=
    `${LEAVES.length} ${NOUN} · ${GROUPS.length} clusters · ${EDGES.length} links · ${XCOUNT} cross-ties`;
}

/* ============================== HOVER CARD ============================== */
const hc=document.getElementById('hc');
function showHC(n,mx,my){
  const col=n.type==='root'?ACCENT_DEEP:CAT[n.cat].color;
  const KICK=DATA.meta.kickers||{};
  let kick=n.type==='hub'?`${KICK.hub||'Department'}${n.ftype?' · '+(TYPE_LABEL[n.ftype]||''):''}`
    :n.type==='group'?(n.ftype?`${TYPE_LABEL[n.ftype]||'Cluster'}`:(KICK.group||'Cluster'))
    :n.type==='root'?'The center':`${CAT[n.cat].name} · ${n.tag}`;
  if(n.type==='leaf'&&n.xotype&&TYPE_BY_ID[n.xotype])kick+=` · ${TYPE_BY_ID[n.xotype].label}`;
  const art=`linear-gradient(155deg, ${hexA(col,.24)}, ${hexA(col,.03)} 68%)`;
  let rows='';
  if(n.type==='leaf'){
    rows=`<dl>
      <dt>${esc(DATA.meta.leafDateLabel||'Born')}</dt><dd>${fmtDate(n.date)}</dd>
      <dt>Where</dt><dd class="mono">${esc(n.path)}</dd>
      <dt>Ties</dt><dd>${n.degree-1} connection${n.degree-1===1?'':'s'} · ${esc(byId.get(n.group).label)}</dd>
    </dl>`;
  }else if(n.type==='group'){
    const kids=LEAVES.filter(l=>l.group===n.id);
    const d0=kids.reduce((m,x)=>x.date<m?x.date:m,'9999'),d1=kids.reduce((m,x)=>x.date>m?x.date:m,'0000');
    rows=`<dl><dt>Holds</dt><dd>${kids.length} ${NOUN}</dd>
      <dt>Span</dt><dd>${fmtMY(+new Date(d0))} to ${fmtMY(+new Date(d1))}</dd></dl>`;
  }else{
    const kids=n.type==='hub'?LEAVES.filter(l=>l.cat===n.cat):LEAVES;
    rows=`<dl><dt>Holds</dt><dd>${kids.length} ${NOUN}</dd></dl>`;
  }
  hc.innerHTML=`
    <div class="art" style="background:${art}">
      <div class="kicker">${esc(kick)}</div>
      <h5>${esc(n.label)}</h5>
      ${n.type==='leaf'?'':`<div class="sub">${esc((n.blurb||'').split('. ')[0])}</div>`}
    </div>
    ${rows}
    <div class="foot">${n.type==='group'?'Click to focus · Double-click to open or close':'Click to focus · Double-click to expand'}</div>`;
  hc.classList.add('is-on');
  const r=hc.getBoundingClientRect();
  let x=mx+18,y=my+18;
  if(x+r.width>innerWidth-8)x=mx-r.width-18;
  if(y+r.height>innerHeight-8)y=my-r.height-18;
  hc.style.left=Math.max(8,x)+'px';hc.style.top=Math.max(64,y)+'px';
}
function hideHC(){hc.classList.remove('is-on');hoverId=null;}

/* ============================== DETAIL PANEL ============================== */
const panel=document.getElementById('panel');
/* Folder archetypes: every folder is one of five (see space_index.py's
   _TYPE_SHAPE). The panel renders a type-specific overview instead of a
   connections list; payloads without ftype (the sessions space) keep the
   legacy connections panel. */
const TYPE_LABEL={app:'App',readme:'One-pager',docs:'Docs',slides:'Slides',unknown:'Unknown'};
const typeOf=n=>n.ftype?n:(n.type==='leaf'&&byId.get(n.group)?.ftype?byId.get(n.group):null);
const dl=pairs=>{
  const rows=pairs.filter(([,v])=>v!==null&&v!==undefined&&v!=='').map(([k,v])=>`<dt>${esc(k)}</dt><dd>${v}</dd>`).join('');
  return rows?`<dl class="pfacts">${rows}</dl>`:'';
};
const chips=items=>items&&items.length?`<div class="fchips">${items.map(t=>`<span>${esc(t)}</span>`).join('')}</div>`:'';
/* ---- search-result card: the panel reads like a SERP entry — breadcrumb,
   linked title, snippet, one meta line, then "quick links" (sitelinks) to
   the most relevant nodes inside. ---- */
function crumbOf(n){
  if(n.type==='root')return DATA.meta.title||'Space';
  const proj=CAT[n.cat]?.name||'';
  if(n.type==='hub')return `${DATA.meta.title||'Space'} › ${proj}`;
  if(n.type==='group')return [proj,...n.label.split(' · ')].join(' › ');
  const dirs=(n.blurb||'').split('/');dirs.pop();
  return [proj,...dirs].join(' › ');
}
function titleOf(n,carrier){
  const f=carrier?.facts||{};
  if(n.type==='leaf')return n.label;
  return f.title||f.name||n.label;
}
function snippetOf(n,carrier){
  const f=carrier?.facts||{},t=carrier?.ftype;
  /* a leaf is normally a plain FILE whose type comes from its parent
     folder (carrier is the group); in the Environments space each leaf is
     a whole classified project and carries its own ftype (carrier===n) —
     fall through to the same title/description rendering a folder gets. */
  if(n.type==='leaf'&&carrier!==n)return `${n.tag} file in ${esc(byId.get(n.group)?.label||'')} · ${fmtDate(n.date)}.`;
  if(t==='readme'&&f.excerpt)return f.excerpt;
  if(t==='app'&&f.description)return f.description;
  if(t==='docs')return `Documentation${f.pages?`: ${f.pages} page${f.pages===1?'':'s'}`:''}${f.sections&&f.sections.length?` across ${f.sections.join(', ')}`:''}.`;
  if(t==='slides'&&f.decks&&f.decks.length)return `Deck${f.decks.length===1?'':'s'}: ${f.decks.map(d=>d.name).join(', ')}.`;
  return n.blurb||'';
}
function metaLineOf(carrier){
  const f=carrier?.facts||{},t=carrier?.ftype,parts=[];
  if(t)parts.push(TYPE_LABEL[t]||t);
  if(t==='app'&&f.language)parts.push(f.language);
  if(t==='docs'&&f.pages)parts.push(`${f.pages} pages`);
  if(t==='readme'&&f.words)parts.push(`${f.words.toLocaleString()} words`);
  if(t==='slides'&&f.decks){const s=f.decks.reduce((a,d)=>a+(d.slides||0),0);parts.push(`${f.decks.length} deck${f.decks.length===1?'':'s'}${s?` · ${s} slides`:''}`);}
  if(f.files)parts.push(`${f.files} files`);
  if(t==='app'&&f.tests)parts.push(`${f.tests} tests`);
  if(f.types)Object.entries(f.types).sort((a,b)=>b[1]-a[1]).slice(0,3)
    .forEach(([k,v])=>parts.push(`${(TYPE_LABEL[k]||k).toLowerCase()} ×${v}`));
  return parts.join(' · ');
}
const _KEY_NAMES=['readme','index','package.json','pyproject.toml','main','server','app'];
function _keyScore(l){
  const base=l.label.toLowerCase();
  for(let i=0;i<_KEY_NAMES.length;i++)if(base.startsWith(_KEY_NAMES[i]))return 100-i;
  return Math.min(40,(l.degree||1)*4)+(l.date?+new Date(l.date)/1e13:0);
}
function quickLinks(n){
  let items=[];
  if(n.type==='group')items=LEAVES.filter(l=>l.group===n.id)
    .sort((a,b)=>_keyScore(b)-_keyScore(a));
  else if(n.type==='hub')items=GROUPS.filter(g=>g.cat===n.cat)
    .sort((a,b)=>((b.facts?.files||0)-(a.facts?.files||0)));
  else if(n.type==='root')items=[...HUBS].sort((a,b)=>((b.facts?.files||0)-(a.facts?.files||0)));
  else if(n.type==='leaf'){
    const parent=byId.get(n.group);
    items=[...(parent?[parent]:[]),...LEAVES.filter(l=>l.group===n.group&&l.id!==n.id)
      .sort((a,b)=>_keyScore(b)-_keyScore(a))];
  }
  return items.slice(0,6);
}
function quickLinksHtml(n){
  const links=quickLinks(n);
  if(!links.length)return '';
  const rows=links.map(o=>{
    const meta=o.type==='leaf'?`${o.tag}${o.date?' · '+o.date.slice(0,7):''}`
      :o.type==='group'?`${(TYPE_LABEL[o.ftype]||'folder').toLowerCase()} · ${o.facts?.files||''} files`
      :`${(TYPE_LABEL[o.ftype]||'project').toLowerCase()}`;
    return `<button class="qlink" data-id="${o.id}">
      <span class="qname">${esc(o.type==='leaf'?o.label:o.label)}</span>
      <span class="qmeta">${esc(meta)}</span>
    </button>`;
  }).join('');
  return `<div class="psec"><h4>Quick links</h4><div class="sitelinks">${rows}</div></div>`;
}
function resultCard(n,carrier){
  const snip=snippetOf(n,carrier);
  let meta=metaLineOf(carrier);
  const xt=n.xotype&&TYPE_BY_ID[n.xotype];
  if(xt)meta=meta?`${meta} · ${xt.label}`:xt.label;
  return `<div class="gres">
    <div class="rcrumb">${esc(crumbOf(n))}</div>
    <button class="rtitle" data-id="${n.id}" title="Zoom to this node">${esc(titleOf(n,carrier))}</button>
    ${meta?`<div class="rmeta">${esc(meta)}</div>`:''}
    ${snip?`<p class="rsnip">${esc(snip)}</p>`:''}
  </div>`;
}
function openPanel(n){
  const col=n.type==='root'?ACCENT_DEEP:CAT[n.cat].color;
  const carrier=typeOf(n);
  const tlabel=carrier?TYPE_LABEL[carrier.ftype]||'Unknown':null;
  const kick=n.type==='hub'?`${(DATA.meta.kickers||{}).hub||'Department'}${tlabel?' · '+tlabel:''} · ${LEAVES.filter(l=>l.cat===n.cat).length} ${NOUN}`
    :n.type==='group'?`${CAT[n.cat].name} · ${(tlabel||((DATA.meta.kickers||{}).group||'cluster')).toLowerCase()}`
    :n.type==='root'?'The center'
    :carrier===n?`${CAT[n.cat].name} · ${tlabel}`  /* leaf is itself the classified thing (Environments) */
    :`${CAT[n.cat].name} · ${n.tag}${tlabel?' · in '+tlabel.toLowerCase()+' folder':''}`;
  let body;
  if(n.type==='root'&&GROUPS.some(g=>g.ftype)){
    /* the center: result card + census + the biggest projects as sitelinks */
    const counts={};GROUPS.forEach(g=>{counts[g.ftype||'unknown']=(counts[g.ftype||'unknown']||0)+1;});
    body=`${resultCard(n,null)}
      <div class="psec"><h4>Folder types</h4>${dl(Object.entries(TYPE_LABEL).map(([t,l])=>[l,counts[t]||null]))}</div>
      ${quickLinksHtml(n)}`;
  }else if(carrier){
    body=`${resultCard(n,carrier)}${quickLinksHtml(n)}`;
  }else{
    /* legacy panel (sessions space): blurb + connections */
    const conns=n.adj
      .filter(({other})=>byId.get(other).type!=='root'||n.type==='hub')
      .sort((p,q)=>(p.e.kind==='x'?0:1)-(q.e.kind==='x'?0:1))
      .slice(0,24)
      .map(({e,other})=>{
        const o=byId.get(other);
        let rel;
        if(e.kind==='x')rel=(e.s===n.id?'':'← ')+e.label;
        else rel=o.type==='group'||o.type==='hub'||o.type==='root'?'part of':'holds';
        return `<button class="conn" data-id="${o.id}">
          <span class="cdot" style="background:${o.type==='root'?ACCENT_DEEP:CAT[o.cat]?.color||'#e9e4d9'}"></span>
          <span>${esc(o.label)}</span>
          <span class="rel">${esc(rel)}</span>
          <span class="yr">${o.date?o.date.slice(0,7):''}</span>
        </button>`;
      }).join('');
    body=`<div class="psec"><h4>About</h4><p>${esc(n.blurb||'')}</p></div>
      ${conns?`<div class="psec"><h4>Connections</h4>${conns}</div>`:''}`;
  }
  const serp=carrier||(n.type==='root'&&GROUPS.some(g=>g.ftype));
  document.getElementById('panel-scroll').innerHTML=`
    <div class="poster${serp?' slim':''}" style="background:radial-gradient(120% 100% at 20% 0%, ${hexA(col,.20)}, transparent 62%)">
      <div class="kicker">${esc(kick)}</div>
      ${serp?'':`<h3>${esc(n.label)}</h3>
      ${n.date?`<div class="sub">${fmtDate(n.date)}</div>`:''}
      ${n.path?`<div class="path">${esc(n.path)}</div>`:''}`}
    </div>
    ${body}
    <div class="pacts">
      ${n.type!=='root'?`<button data-act="timeline">Show on timeline</button>`:''}
      ${n.type==='group'||n.type==='hub'?`<button data-act="root">Open as root</button>`:''}
      <button data-act="from">Path from here</button>
      <button data-act="to">Path to here</button>
    </div>
    <div class="psec mtl" id="panel-mini-tl" hidden></div>`;
  panel.classList.add('is-open');
  panel.dataset.id=n.id;
  /* Projects leaves get a content preview; sessions leaves have no file path. */
  if(n.type==='leaf')openLeafPreview(n,{workspace:DATA.meta?.workspace});
  else closePreview();
  mountMiniTimeline(n);
}
function closePanel(){
  panel.classList.remove('is-open');
  closePreview();
}
document.getElementById('panel-close').addEventListener('click',()=>{clearFocus();clearPath();});
panel.addEventListener('click',e=>{
  const c=e.target.closest('.conn,.qlink,.rtitle');
  if(c&&c.dataset.id){
    const n=byId.get(c.dataset.id);
    if(!n)return;
    ensureShown(n);
    go('graph');
    select(n.id,1);
    pulseN={id:n.id,t0:performance.now()};
    return;
  }
  const a=e.target.closest('[data-act]');
  if(!a)return;
  const n=byId.get(panel.dataset.id);
  if(a.dataset.act==='timeline'){traceOnTimeline(n);}
  else if(a.dataset.act==='root'){setRoot(n.id);}
  else if(a.dataset.act==='from'){document.getElementById('six-a').value=n.label;sixA=n;go('six');}
  else if(a.dataset.act==='to'){document.getElementById('six-b').value=n.label;sixB=n;go('six');}
});
function ensureShown(n){
  if(n.type==='leaf'){
    if(deptFilter&&n.cat!==deptFilter){
      deptFilter=null;
      [...chipsEl.children].forEach((x,i)=>x.classList.toggle('is-on',i===0));
      toast('Filter cleared to reach '+n.label);
    }
    if(!expanded.get(n.group)){setExp(byId.get(n.group),true);reheat(.4);}
  }
}

/* ============================== SEARCH ============================== */
function rankMatches(q){
  q=q.trim().toLowerCase();
  if(!q)return[];
  const out=[];
  for(const n of NODES){
    if(n.type==='root')continue;
    const s=n.label.toLowerCase();
    let sc=-1;const idx=s.indexOf(q);
    if(idx===0)sc=0;
    else if(idx>0&&/\W/.test(s[idx-1]))sc=1;
    else if(idx>0)sc=2;
    else if((n.tag||'').toLowerCase().includes(q))sc=3;
    else if((n.blurb||'').toLowerCase().includes(q))sc=4;
    if(sc>=0)out.push([sc,n,idx]);
  }
  out.sort((a,b)=>a[0]-b[0]||(b[1].degree-a[1].degree)||a[1].label.length-b[1].label.length);
  return out.slice(0,8);
}
function acRow(n,idx,q){
  const col=n.cat?CAT[n.cat].color:'#e9e4d9';
  const name=idx>=0
    ?esc(n.label.slice(0,idx))+'<em>'+esc(n.label.slice(idx,idx+q.length))+'</em>'+esc(n.label.slice(idx+q.length))
    :esc(n.label);
  let meta=n.type==='hub'?'dept':n.type==='group'?'cluster':n.tag;
  if(n.type==='leaf'&&n.xotype&&TYPE_BY_ID[n.xotype])meta+=` · ${TYPE_BY_ID[n.xotype].label.toLowerCase()}`;
  const dia=n.shape==='diamond'?' dia':n.shape==='stack'?' stk':n.shape==='slab'?' slb':'';
  return {col,name,meta,dia};
}
function wireAC(input,acEl,onPick){
  let items=[],act=-1;
  const render=q=>{
    if(!items.length&&q){acEl.innerHTML=`<div class="empty">No match in this workspace<small>${LEAVES.length} ${NOUN} mapped</small></div>`;acEl.classList.add('is-open');return;}
    acEl.innerHTML=items.map(([sc,n,idx],i)=>{
      const r=acRow(n,idx,q);
      return `<button class="${i===act?'is-active':''}" data-i="${i}">
        <span class="tdot${r.dia}" style="background:${r.col}"></span><span>${r.name}</span><span class="meta">${esc(r.meta||'')}</span></button>`;
    }).join('');
    acEl.classList.toggle('is-open',items.length>0);
  };
  const pickI=i=>{
    if(i<0||i>=items.length)return;
    const n=items[i][1];
    acEl.classList.remove('is-open');items=[];act=-1;
    input.value=n.label;
    onPick(n);
  };
  input.addEventListener('input',()=>{items=rankMatches(input.value);act=items.length?0:-1;render(input.value.trim().toLowerCase());});
  input.addEventListener('keydown',e=>{
    if(e.key==='ArrowDown'){act=(act+1)%items.length;render(input.value.toLowerCase());e.preventDefault();}
    else if(e.key==='ArrowUp'){act=(act-1+items.length)%items.length;render(input.value.toLowerCase());e.preventDefault();}
    else if(e.key==='Enter'){pickI(act>=0?act:0);e.preventDefault();}
    else if(e.key==='Escape'){acEl.classList.remove('is-open');items=[];input.blur();}
  });
  input.addEventListener('blur',()=>setTimeout(()=>acEl.classList.remove('is-open'),140));
  acEl.addEventListener('pointerdown',e=>{
    const b=e.target.closest('button');
    if(b){e.preventDefault();pickI(+b.dataset.i);}
  });
}
wireAC(document.getElementById('q'),document.getElementById('qac'),n=>{
  ensureShown(n);
  go('graph');
  clearPath();
  select(n.id,1,false);
  const kT=n.type==='leaf'?2.2:1.3;
  flyTo(n.x+focusPanOffset(kT),n.y,kT);
  pulseN={id:n.id,t0:performance.now()};
  toast('Found '+n.label);
  document.getElementById('q').value='';
});
wireAC(document.getElementById('root-q'),document.getElementById('root-ac'),n=>setRoot(n.id));
document.getElementById('root-q').addEventListener('keydown',e=>{
  if(e.key==='Escape')closeRootDD();
});

/* ============================== VIEWS + GLOBAL KEYS ==============================
   Tab/section toggling now lives in core/registry.js. The atlas keeps only
   its internal notion of which of its lenses is active — it gates the sim
   loop and timeline rebuilds — plus the search-focus and clear keys. */
let view='graph';
/* Timeline is always the vertical branching-growth renderer now, in every
   space (commit_timeline.js dispatches to a per-space renderer — growth
   trunk / commit graph / braided streams). It owns #tplot itself once
   .is-commits is set on #view-time — see timeline.css for what that class
   hides (the old artifact-beeswarm's scrub/play controls, now unused by
   any space but left in place rather than torn out). */
const commitTimelineMode=()=>true;
hooks.setActiveView=v=>{
  view=v;
  hideHC();
  if(v==='graph'&&GW<50)resize(); /* booted while hidden (deep link): size the canvas now */
  if(v==='time'){
    document.getElementById('view-time')?.classList.add('is-commits');
    mountCommitTimeline(graphMode());
  }
};

/* ---- Files: Graph | List toggle. List renders the Overview's tree content
   (workspace tree in the projects space, per-runtime session stores in the
   sessions space) fetched from the overview endpoints; Graph is the sim. ---- */
{
  const FM_KEY='space.filesMode';
  const fmode=()=>{try{return localStorage.getItem(FM_KEY)==='list'?'list':'graph';}catch(_e){return 'graph';}};
  const fEl=document.getElementById('fmode');
  const listEl=document.getElementById('flist');
  let listLoaded=false;
  async function loadList(){
    if(listLoaded||!listEl)return;
    listLoaded=true;
    /* shared with the whiteboard's Files card — see core/fileslist.js */
    const ok=await renderFilesList(listEl,{sessions:graphMode()==='sessions'});
    if(!ok)listLoaded=false;  /* failed loads may retry on next toggle */
  }
  function applyFmode(m){
    const g=document.getElementById('view-graph');
    if(!g)return;
    g.classList.toggle('is-list',m==='list');
    if(m==='list')loadList();
    if(fEl)[...fEl.children].forEach(b=>b.classList.toggle('is-on',b.dataset.fm===m));
  }
  if(fEl&&!fEl.childElementCount){
    [['graph','Graph'],['list','List']].forEach(([id,label])=>{
      const b=document.createElement('button');
      b.textContent=label;b.dataset.fm=id;
      b.addEventListener('click',()=>{
        if(fmode()===id)return;
        try{localStorage.setItem(FM_KEY,id);}catch(_e){}
        applyFmode(id);
      });
      fEl.appendChild(b);
    });
    applyFmode(fmode());
  }
}
addEventListener('keydown',e=>{
  const typing=/INPUT|TEXTAREA/.test(document.activeElement?.tagName||'');
  if(e.key==='/'&&!typing){e.preventDefault();document.getElementById('q').focus();return;}
  if(typing)return;
  if(e.key==='Escape'){clearFocus();clearPath();hideHC();}
});

/* ============================== TIMELINE ============================== */
const T0=+new Date(DATA.timeline.start+'T00:00:00'),T1=+new Date(DATA.timeline.end+'T00:00:00');
const SVGNS='http://www.w3.org/2000/svg';
let tNow=T1,tPlaying=false,tTrace=null;
const tplot=document.getElementById('tplot');
const tsvg=document.createElementNS(SVGNS,'svg');
tplot.appendChild(tsvg);
const MILES=DATA.milestones;
function buildTimeline(){
  const W=tplot.clientWidth,H=tplot.clientHeight;
  if(W<50||H<50)return;
  tsvg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  tsvg.innerHTML='';
  const M={t:26,r:20,b:24,l:96};
  const xOf=t=>M.l+(t-T0)/(T1-T0)*(W-M.l-M.r);
  const lanes=Object.keys(CAT);
  const laneH=(H-M.t-M.b)/lanes.length;
  /* lane bands + labels */
  lanes.forEach((cat,i)=>{
    const y=M.t+i*laneH;
    const band=document.createElementNS(SVGNS,'rect');
    band.setAttribute('x',M.l-8);band.setAttribute('y',y+3);
    band.setAttribute('width',W-M.l-M.r+16);band.setAttribute('height',laneH-6);
    band.setAttribute('fill',hexA(CAT[cat].color,.04));band.setAttribute('rx',8);
    tsvg.appendChild(band);
    const lb=document.createElementNS(SVGNS,'text');
    lb.setAttribute('x',M.l-16);lb.setAttribute('y',y+laneH/2+4);
    lb.setAttribute('text-anchor','end');
    lb.setAttribute('style',`font:italic 500 13px ${SERIF};fill:${hexA(CAT[cat].color,.95)}`);
    lb.textContent=CAT[cat].name;
    tsvg.appendChild(lb);
  });
  /* month grid */
  let d=new Date(T0);
  while(+d<T1){
    const x=xOf(+d);
    const ln=document.createElementNS(SVGNS,'line');
    ln.setAttribute('x1',x);ln.setAttribute('x2',x);
    ln.setAttribute('y1',M.t-4);ln.setAttribute('y2',H-M.b);
    ln.setAttribute('stroke',d.getMonth()===0?'rgba(233,228,217,.13)':'rgba(233,228,217,.05)');
    ln.setAttribute('stroke-dasharray','1 4');
    tsvg.appendChild(ln);
    const tx=document.createElementNS(SVGNS,'text');
    tx.setAttribute('x',x);tx.setAttribute('y',M.t-10);
    tx.setAttribute('text-anchor','middle');
    tx.setAttribute('style',`font:400 8.5px ${MONO};letter-spacing:.08em;fill:#56534b`);
    tx.textContent=d.toLocaleDateString('en-US',{month:'short'}).toUpperCase();
    tsvg.appendChild(tx);
    d=new Date(d.getFullYear(),d.getMonth()+1,1);
  }
  /* milestone pips */
  MILES.forEach(m=>{
    const x=xOf(+new Date(m.d+'T00:00:00'));
    const c=document.createElementNS(SVGNS,'circle');
    c.setAttribute('cx',x);c.setAttribute('cy',H-M.b+10);c.setAttribute('r',2.4);
    c.setAttribute('fill','#3a4136');c.dataset.milestone='1';c.dataset.t=+new Date(m.d+'T00:00:00');
    tsvg.appendChild(c);
  });
  /* beeswarm dots */
  lanes.forEach((cat,li)=>{
    const ns=LEAVES.filter(n=>n.cat===cat).sort((a,b)=>a.date<b.date?-1:1);
    const placed=[];
    const baseY=M.t+li*laneH+laneH/2;
    const maxRow=Math.max(1,Math.floor((laneH/2-10)/13));
    ns.forEach(n=>{
      n.tx=xOf(+new Date(n.date+'T00:00:00'));
      const hit=r=>placed.some(p=>Math.abs(p.tx-n.tx)<14&&p.row===r);
      let row=0,guard=0;
      while(hit(row)&&guard++<200){
        row=row>0?-row:-row+1;
        if(Math.abs(row)>maxRow){n.tx+=12;row=0;}
      }
      n.row=row;n.ty=baseY+row*13;
      placed.push(n);
    });
  });
  const dotsG=document.createElementNS(SVGNS,'g');
  dotsG.setAttribute('id','tdots');
  tsvg.appendChild(dotsG);
  LEAVES.forEach(n=>{
    const col=CAT[n.cat].color;
    const r=3.2+Math.min(2.6,(n.degree-1)*.5);
    let el;
    if(n.shape==='diamond'){
      el=document.createElementNS(SVGNS,'rect');
      const s=r*1.6;
      el.setAttribute('x',n.tx-s/2);el.setAttribute('y',n.ty-s/2);
      el.setAttribute('width',s);el.setAttribute('height',s);
      el.setAttribute('transform',`rotate(45 ${n.tx} ${n.ty})`);
      el.setAttribute('fill',col);
    }else if(n.shape==='ring'){
      el=document.createElementNS(SVGNS,'circle');
      el.setAttribute('cx',n.tx);el.setAttribute('cy',n.ty);el.setAttribute('r',r-.5);
      el.setAttribute('fill','none');el.setAttribute('stroke',col);el.setAttribute('stroke-width',1.5);
    }else if(n.shape==='slab'){
      el=document.createElementNS(SVGNS,'rect');
      const w=r*2.2,h=r*1.4;
      el.setAttribute('x',n.tx-w/2);el.setAttribute('y',n.ty-h/2);
      el.setAttribute('width',w);el.setAttribute('height',h);
      el.setAttribute('fill',col);
    }else if(n.shape==='stack'){
      el=document.createElementNS(SVGNS,'rect');
      const s=r*1.55;
      el.setAttribute('x',n.tx-s/2);el.setAttribute('y',n.ty-s/2);
      el.setAttribute('width',s);el.setAttribute('height',s);
      el.setAttribute('fill',col);
    }else{
      el=document.createElementNS(SVGNS,'circle');
      el.setAttribute('cx',n.tx);el.setAttribute('cy',n.ty);el.setAttribute('r',r);
      el.setAttribute('fill',col);
    }
    el.dataset.id=n.id;
    el.style.cursor='pointer';
    /* XO data-type weight: fill/stroke-opacity multiplies with the scrub's
       style.opacity, so dim types stay dim through timeline playback */
    const ta=typeAlpha(n);
    if(ta<1){el.setAttribute('fill-opacity',ta);el.setAttribute('stroke-opacity',ta);}
    dotsG.appendChild(el);
    n.tEl=el;
  });
  /* trace layer + sweep */
  const traceG=document.createElementNS(SVGNS,'g');
  traceG.setAttribute('id','ttrace');
  tsvg.insertBefore(traceG,dotsG);
  const sweep=document.createElementNS(SVGNS,'line');
  sweep.setAttribute('id','tsweep');
  sweep.setAttribute('y1',M.t-6);sweep.setAttribute('y2',H-M.b);
  sweep.setAttribute('stroke',ACCENT);sweep.setAttribute('stroke-width',1.2);
  sweep.setAttribute('stroke-dasharray','2 4');sweep.setAttribute('opacity',.55);
  tsvg.appendChild(sweep);
  tsvg._xOf=xOf;
  renderTimelineState();
}
function renderTimelineState(){
  const xOf=tsvg._xOf;if(!xOf)return;
  document.getElementById('tsweep')?.setAttribute('x1',xOf(tNow));
  document.getElementById('tsweep')?.setAttribute('x2',xOf(tNow));
  LEAVES.forEach(n=>{
    if(!n.tEl)return;
    const born=+new Date(n.date+'T00:00:00')<=tNow;
    let op=born?.8:.06;
    if(tTrace){
      const inTrace=tTrace.ids.has(n.id);
      op=inTrace?(born?1:.06):(born?.10:.03);
    }
    n.tEl.setAttribute('opacity',op);
  });
  tsvg.querySelectorAll('[data-milestone]').forEach(c=>{
    c.setAttribute('fill',+c.dataset.t<=tNow?ACCENT_DEEP:'#33362f');
  });
  /* readout + milestone caption */
  document.getElementById('treadout').textContent=fmtMY(tNow);
  const m=[...MILES].reverse().find(x=>+new Date(x.d+'T00:00:00')<=tNow);
  const mEl=document.getElementById('tmilestone');
  mEl.textContent=m?m.t:'';
  mEl.style.opacity=m?1:0;
  document.getElementById('tscrub').value=Math.round((tNow-T0)/(T1-T0)*1000);
}
/* Maps a clicked graph node to its commit/edit history, wherever one exists:
     Projects space     — leaf/group/hub all share one project (n.cat, p_<id>
                           prefix stripped); filter commits.json by project.
     Environments space  — a leaf IS a single project (one-leaf-per-project
                           schema: n.id is the raw project id) → filter
                           environment_commits.json by project; a hub/group
                           is a whole cluster (n.cat) → filter by category.
     Sessions space       — groups by runtime, not project, so there is no
                           per-node id shared with session_diffs.json's
                           events; a hub (runtime) matches by author (the
                           runtime's display label, same string both sides);
                           a leaf/group resolves via its group's label (the
                           display project name — session_telemetry and
                           commit_diffs both derive it from the same cwd
                           basename, so the strings line up in practice).
   Returns {url,field,value,label} or null when nothing is resolvable (root,
   or a case with no shared id). */
/* `value`/`field` is always the MOST SPECIFIC filter available — what the
   mini preview uses via getGroupHistory, which filters raw events directly
   and doesn't care what axis the full Timeline's chips are built on.
   `chipValue` is what "Open full timeline" hands to selectTimelineGroup —
   it MUST match that space's cfg.groupField (see commit_timeline.js's
   SPACE_CONFIG): 'project' for Projects/Sessions, but 'category' for
   Environments, whose chips are clusters, not individual projects. A leaf
   click in Environments is the one case those two diverge (project-level
   preview, cluster-level chip) — chipValue falls back to the leaf's own
   cluster there. Sessions groups by runtime for hubs, which the full
   Timeline's project-chip axis has no equivalent for at all; chipValue is
   null there, so "Open full timeline" just opens unfiltered. */
function resolveTimelineTarget(n){
  const mode=graphMode();
  if(n.type==='root')return null;
  if(mode==='output'){
    const pid=(n.cat||'').replace(/^p_/,'');
    return pid?{url:'data/commits.json',field:'project',value:pid,chipValue:pid,label:CAT[n.cat]?.name||pid}:null;
  }
  if(mode==='environments'){
    if(n.type==='leaf')return {url:'data/environment_commits.json',field:'project',value:n.id,chipValue:n.cat,label:n.label};
    return n.cat?{url:'data/environment_commits.json',field:'category',value:n.cat,chipValue:n.cat,label:CAT[n.cat]?.name||n.cat}:null;
  }
  if(mode==='sessions'){
    if(n.type==='hub')return {url:'data/session_diffs.json',field:'author',value:n.label,chipValue:null,label:n.label};
    const g=n.type==='group'?n:byId.get(n.group);
    return g?{url:'data/session_diffs.json',field:'project',value:g.label,chipValue:g.label,label:g.label}:null;
  }
  return null;
}
function traceOnTimeline(n){
  /* selectTimelineGroup before go('time'): the registry's switchTo() is
     async and conditionally awaits a mount step on Timeline's first visit,
     so there is no reliable "call go(), then call this after" ordering —
     setting the pending selection first means mountCommitTimeline picks it
     up atomically whenever it actually runs (see commit_timeline.js). */
  const target=resolveTimelineTarget(n);
  if(target&&target.chipValue)selectTimelineGroup(target.chipValue);
  go('time');
}

/* Inline mini split-trunk under "Show on timeline" in the panel — the same
   split-silhouette representation as the Environments Timeline, in its
   horizontal orientation (time left-to-right suits the wide, short panel),
   pre-filtered to just this one node. Guards against the panel having moved
   on to a different node by the time the async fetch resolves
   (panel.dataset.id is the source of truth, set synchronously by openPanel). */
async function mountMiniTimeline(n){
  const host=document.getElementById('panel-mini-tl');
  if(!host)return;
  const target=resolveTimelineTarget(n);
  if(!target){host.hidden=true;return;}
  host.hidden=false;
  host.innerHTML='<div class="mtl-state">Loading history…</div>';
  const nodeId=n.id;
  let hist;
  try{hist=await getGroupHistory(target.url,target.field,target.value,target.label);}
  catch(err){console.error('mini timeline fetch failed:',err);hist={error:'unavailable'};}
  if(panel.dataset.id!==nodeId)return;  // panel moved to a different node meanwhile
  if(hist.error){host.innerHTML=`<div class="mtl-state">History unavailable · ${esc(hist.error)}</div>`;return;}
  if(hist.empty){host.innerHTML='<div class="mtl-state">No history yet.</div>';return;}
  host.innerHTML='<div class="mtl-chart"></div>';
  const chart=host.firstElementChild;
  const W=Math.max(160,chart.clientWidth||304),H=112;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.setAttribute('class','mtl-svg');
  chart.appendChild(svg);
  const opts={
    onHover:(e,x,y)=>showPopoverIn(chart,e,x,y),
    onMove:(x,y)=>movePopoverIn(chart,x,y),
    onLeave:()=>hidePopoverSoon(),
    hexA,esc,
    colorIns:'#7fd08a',colorDel:'#c8674c',colorAccent:ACCENT,
    colorInk:'#e9e4d9',colorInk3:'#7d786d',colorLine:'rgba(233,228,217,.10)',
    fmtDate:commitDtfmt,
    orient:'h',
  };
  try{
    renderSplitTrunk(svg,W,H,{groups:[hist.group],t0:hist.t0,t1:hist.t1},opts);
  }catch(err){
    console.error('mini timeline render failed:',err);
    host.innerHTML='<div class="mtl-state">Could not render history.</div>';
    return;
  }
  const openBtn=document.createElement('button');
  openBtn.className='mtl-open';
  openBtn.textContent='Open full timeline →';
  openBtn.addEventListener('click',()=>traceOnTimeline(n));
  host.appendChild(openBtn);
}
function drawTrace(){
  const g=tsvg.querySelector('#ttrace');
  if(!g)return;
  g.innerHTML='';
  if(!tTrace||tTrace.list.length<2){renderTimelineState();return;}
  const pts=tTrace.list.map(n=>[n.tx,n.ty]);
  let path=`M ${pts[0][0]} ${pts[0][1]}`;
  for(let i=1;i<pts.length;i++){
    const [x0,y0]=pts[i-1],[x1,y1]=pts[i];
    const mx=(x0+x1)/2;
    path+=` C ${mx} ${y0}, ${mx} ${y1}, ${x1} ${y1}`;
  }
  const p=document.createElementNS(SVGNS,'path');
  p.setAttribute('d',path);p.setAttribute('fill','none');
  p.setAttribute('stroke',ACCENT);p.setAttribute('stroke-width',1.3);p.setAttribute('opacity',.7);
  g.appendChild(p);
  /* labels: alternate above/below, and step outward when several share an x window */
  const win=[];
  tTrace.list.forEach((n,i)=>{
    const near=win.filter(w=>Math.abs(w-n.tx)<74).length;
    win.push(n.tx);
    const up=i%2===0;
    const step=Math.floor(near/2)*11;
    const t=document.createElementNS(SVGNS,'text');
    t.setAttribute('x',n.tx);
    t.setAttribute('y',up?n.ty-10-step:n.ty+16+step);
    t.setAttribute('text-anchor','middle');
    t.setAttribute('style',`font:400 9.5px ${SERIF};fill:#b3ada0`);
    t.textContent=n.label;
    g.appendChild(t);
  });
  renderTimelineState();
}
function clearTrace(){
  tTrace=null;
  document.getElementById('tclear').hidden=true;
  document.getElementById('tsub').textContent='Scrub through the workspace as it grew. Open any cluster from the graph to watch its run unfold here.';
  const g=tsvg.querySelector('#ttrace');if(g)g.innerHTML='';
  renderTimelineState();
}
document.getElementById('tclear').addEventListener('click',clearTrace);
document.getElementById('tscrub').addEventListener('input',e=>{
  stopPlay();
  tNow=T0+(+e.target.value/1000)*(T1-T0);
  renderTimelineState();
});
let playRAF=null;
function startPlay(){
  tPlaying=true;
  document.querySelector('#tplay span').textContent='Pause';
  const step=()=>{
    tNow+=(T1-T0)/(60*16);
    if(tNow>=T1){tNow=T1;stopPlay();}
    renderTimelineState();
    if(tPlaying)playRAF=requestAnimationFrame(step);
  };
  cancelAnimationFrame(playRAF);playRAF=requestAnimationFrame(step);
}
function stopPlay(){
  tPlaying=false;cancelAnimationFrame(playRAF);
  document.querySelector('#tplay span').textContent='Play';
}
document.getElementById('tplay').addEventListener('click',()=>{
  if(tPlaying){stopPlay();return;}
  if(tNow>=T1-3600000)tNow=T0;
  startPlay();
});
document.getElementById('tsix')?.addEventListener('click',()=>go('six'));
tsvg.addEventListener('pointermove',e=>{
  const t=e.target;
  if(t.dataset&&t.dataset.id){showHC(byId.get(t.dataset.id),e.clientX,e.clientY);}
  else hideHC();
});
tsvg.addEventListener('pointerleave',hideHC);
tsvg.addEventListener('click',e=>{
  const t=e.target;
  if(t.dataset&&t.dataset.id){
    const n=byId.get(t.dataset.id);
    ensureShown(n);
    go('graph');
    select(n.id,1);
    pulseN={id:n.id,t0:performance.now()};
  }
});

/* ============================== SIX DEGREES ============================== */
let sixA=null,sixB=null,sixPath=null;
wireAC(document.getElementById('six-a'),document.getElementById('six-aac'),n=>{sixA=n;});
wireAC(document.getElementById('six-b'),document.getElementById('six-bac'),n=>{sixB=n;});
const COST={x:1,rg:1.4,hg:2.4,root:4.5};
function shortest(aId,bId){
  const dist=new Map(),prev=new Map(),Q=new Set();
  NODES.forEach(n=>{dist.set(n.id,1e9);Q.add(n.id);});
  dist.set(aId,0);
  while(Q.size){
    let u=null,ud=1e9;
    for(const id of Q){const dd=dist.get(id);if(dd<ud){ud=dd;u=id;}}
    if(u===null||ud===1e9)break;
    Q.delete(u);
    if(u===bId)break;
    for(const{e,other}of byId.get(u).adj){
      if(!Q.has(other))continue;
      const nd=ud+COST[e.kind];
      if(nd<dist.get(other)){dist.set(other,nd);prev.set(other,{id:u,e});}
    }
  }
  if(dist.get(bId)===1e9)return null;
  const hops=[];let cur=bId;
  while(cur!==aId){const p=prev.get(cur);hops.unshift({id:cur,e:p.e});cur=p.id;}
  return[{id:aId,e:null},...hops];
}
function relText(e,fromId){
  if(!e)return'';
  if(e.kind==='x')return e.label;
  if(e.kind==='root')return DATA.meta.rootEdgeLabel||'a department of XO';
  const child=byId.get(e.t),parent=byId.get(e.s);
  return fromId===child.id?`part of ${parent.label}`:`holds ${child.label}`;
}
function runSix(){
  const err=document.getElementById('sixerr');
  err.textContent='';
  if(!sixA||!sixB){err.textContent=`Pick two ${NOUN} first.`;return;}
  if(sixA.id===sixB.id){err.textContent=`That is the same ${NOUN.replace(/s$/,'')}. Try two different names.`;return;}
  sixPath=shortest(sixA.id,sixB.id);
  if(!sixPath){err.textContent=`No route connects ${sixA.label} and ${sixB.label} in this space.`;return;}
  const deg=sixPath.length-1;
  const out=document.getElementById('sixout');
  out.innerHTML=`
    <div class="degline"><b>${deg}</b> degree${deg===1?'':'s'} of separation · ${esc(sixA.label)} to ${esc(sixB.label)}</div>
    <div class="chain">${sixPath.map((h,i)=>{
      const n=byId.get(h.id);
      const col=n.cat?CAT[n.cat].color:'#e9e4d9';
      const meta=n.type==='hub'?((DATA.meta.kickers||{}).hub||'department').toLowerCase():n.type==='group'?((DATA.meta.kickers||{}).group||'cluster').toLowerCase():n.tag;
      const card=`<div class="ncard" style="animation-delay:${i*130}ms">
        <span class="cdot" style="background:${col}"></span>
        <span class="nm">${esc(n.label)}</span><span class="meta">${esc(meta||'')}</span></div>`;
      const link=h.e?`<div class="link" style="animation-delay:${(i-.5)*130}ms">
        <span class="rule"></span><span class="rel">${esc(chainSentence(h.e))}</span></div>`:'';
      return link+card;
    }).join('')}</div>
    <button id="sixtrace">Trace on the graph &rarr;</button>`;
  document.getElementById('sixtrace').addEventListener('click',traceSixOnGraph);
}
function chainSentence(e){
  const s=byId.get(e.s),t=byId.get(e.t);
  if(e.kind==='x')return`${s.label} ${e.label} ${t.label}`;
  if(e.kind==='root')return`${t.label} is ${DATA.meta.rootEdgeLabel||'a department of XO'}`;
  return`${t.label} is part of ${s.label}`;
}
function traceSixOnGraph(){
  if(!sixPath)return;
  clearFocus();
  pathIds=sixPath.map(h=>h.id);
  pathEdges=sixPath.filter(h=>h.e).map(h=>h.e);
  pathIds.forEach(id=>ensureShown(byId.get(id)));
  reheat(.35);
  go('graph');
  pathReveal=performance.now()+350;
  setTimeout(()=>fitNodes(pathIds,240),380);
}
document.getElementById('sixgo').addEventListener('click',runSix);
document.getElementById('sixswap').addEventListener('click',()=>{
  [sixA,sixB]=[sixB,sixA];
  const a=document.getElementById('six-a'),b=document.getElementById('six-b');
  [a.value,b.value]=[b.value,a.value];
});
document.getElementById('sixrand').addEventListener('click',()=>{
  let a,b,p,tries=0;
  do{
    a=LEAVES[Math.floor(Math.random()*LEAVES.length)];
    b=LEAVES[Math.floor(Math.random()*LEAVES.length)];
    p=a!==b?shortest(a.id,b.id):null;
    tries++;
  }while(tries<30&&(!p||p.length<5||a.cat===b.cat));
  sixA=a;sixB=b;
  document.getElementById('six-a').value=a.label;
  document.getElementById('six-b').value=b.label;
  runSix();
});


/* ============================== BOOT ============================== */
function resize(){
  dpr=Math.min(2,devicePixelRatio||1);
  const r=document.getElementById('view-graph').getBoundingClientRect();
  GW=r.width;GH=r.height;
  gcv.width=GW*dpr;gcv.height=GH*dpr;
  gcv.style.width=GW+'px';gcv.style.height=GH+'px';
  if(view==='time'){
    if(commitTimelineMode())resizeCommitTimeline();
    else buildTimeline();
  }
}
addEventListener('resize',resize);
resize();
for(let i=0;i<260;i++)simTick();
simAlpha=.35;
/* initial camera: fit everything, biased right so the intro sits over calm space */
{
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  shownNodes().forEach(n=>{x0=Math.min(x0,n.x);y0=Math.min(y0,n.y);x1=Math.max(x1,n.x);y1=Math.max(y1,n.y);});
  const k=Math.max(.3,Math.min(1.6,.94*Math.min(GW/(x1-x0+140),GH/(y1-y0+140))));
  cam.k=k;
  cam.x=(x0+x1)/2-GW*.11/k;
  cam.y=(y0+y1)/2;
}
function frame(now){
  if(view==='graph'){simTick();drawGraph(now);}
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
renderTimelineState();

}
