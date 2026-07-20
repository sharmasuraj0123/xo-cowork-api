/* Split-silhouette trunk — the honest successor to timeline_growth_trunk.
   The trunk splits at a neutral spine: one side is CUMULATIVE LINES EVER
   ADDED (green envelope), the other CUMULATIVE LINES EVER DELETED (red
   envelope). Each commit steps its own terrace into the silhouette: a green
   riser on the insertion edge and a red riser on the deletion edge at the
   same instant, so churn (+4200/-3900), pure growth (+2000/-0), and purges
   (-6800) each have a distinct visible shape — nothing is netted away.
   Units are readable: both envelopes share one sqrt scale whose gridlines /
   ruler are drawn at TRUE values (1k, 10k, ...) at their compressed
   positions.
   Two orientations, one module:
     opts.orient='h'  — panel mini (305x112): spine horizontal, time
                        left(root) to right(now), insertions above,
                        deletions below. Matches the chosen design mockup.
     default ('v')    — full Environments timeline: time bottom(oldest) to
                        top(now), one trunk per cluster; right side of each
                        spine = added, left = deleted, all trunks on one
                        shared sqrt ruler so widths are comparable.
   Pure function of (svg, W, H, grouped, opts) — no imports, no module
   state; hoverable glyphs route through opts.onHover/onMove/onLeave. */

const SVGNS='http://www.w3.org/2000/svg';
const DAY=864e5;

function ce(tag,attrs){
  const el=document.createElementNS(SVGNS,tag);
  if(attrs)for(const k in attrs)el.setAttribute(k,attrs[k]);
  return el;
}
const clamp=(v,lo,hi)=>v<lo?lo:v>hi?hi:v;
const msOf=iso=>+new Date(iso);
const num0=v=>Number(v)||0;
const fmtK=n=>{
  const a=Math.abs(n);
  return a>=1e6?(a/1e6).toFixed(1)+'M':a>=1e3?(a/1e3).toFixed(1)+'K':String(Math.round(a));
};

function attachHover(el,ev,opts){
  el.style.cursor='pointer';
  el.addEventListener('pointerenter',e=>opts.onHover(ev,e.clientX,e.clientY));
  el.addEventListener('pointermove',e=>opts.onMove(e.clientX,e.clientY));
  el.addEventListener('pointerleave',()=>opts.onLeave());
}

// Density guard, same shape as the other renderers: above `target` marks,
// coalesce contiguous (already date-sorted) slices into one synthesized
// summary event per bucket. Cumulative envelopes are additive, so bucketing
// provably does not change the silhouette — only the riser granularity.
function bucketEvents(events,msArr,target,label){
  const n=events.length;
  const bucketCount=Math.max(1,Math.min(target,n));
  const items=[];
  for(let b=0;b<bucketCount;b++){
    const lo=Math.floor(b*n/bucketCount),hi=Math.floor((b+1)*n/bucketCount);
    if(hi<=lo)continue;
    let ins=0,del=0,files=0;
    for(let i=lo;i<hi;i++){
      ins+=num0(events[i].insertions);del+=num0(events[i].deletions);
      files+=num0(events[i].files_count)||(events[i].files?events[i].files.length:0);
    }
    const count=hi-lo;
    items.push({
      ms:msArr[lo],
      ev:{
        id:`bucket:${label}:${b}`,date:events[lo].date,project:label,project_label:label,
        category:label,worktree:null,title:`${count} commit${count===1?'':'s'} (coalesced)`,
        author:'',sha:null,insertions:ins,deletions:del,files:[],files_count:files,
      },
    });
  }
  return items;
}

/* Per-group prep shared by both orientations: split main vs worktrees,
   bucket the main line to ~1 riser pair per `target` px of time axis, and
   annotate each item with cumulative ins/del before+after. */
function prepGroup(g,target){
  const main=[],byWt=new Map();
  for(const e of (g.events||[])){
    if(e.worktree){
      if(!byWt.has(e.worktree))byWt.set(e.worktree,[]);
      byWt.get(e.worktree).push(e);
    }else main.push(e);
  }
  const mainMs=main.map(e=>msOf(e.date));
  const items=main.length<=target
    ?main.map((e,i)=>({ev:e,ms:mainMs[i]}))
    :bucketEvents(main,mainMs,target,g.label);
  let ci=0,cd=0;
  for(const it of items){
    it.insBefore=ci;it.delBefore=cd;
    ci+=num0(it.ev.insertions);cd+=num0(it.ev.deletions);
    it.insAfter=ci;it.delAfter=cd;
  }
  return {g,items,byWt,totalIns:ci,totalDel:cd};
}

// Cumulative offset (in lines, not px) at an arbitrary instant.
function cumAt(items,ms,key){
  let v=0;
  for(const it of items){
    if(it.ms>ms)break;
    v=it[key];
  }
  return v;
}

/* True-value ruler ticks for a sqrt scale: from the 1-10-100... ladder,
   keep values whose compressed position is far enough from the spine and
   from each other to label at ~7px type. */
function sqrtTicks(k,maxVal,maxOff){
  const out=[];
  let last=0;
  for(const v of [100,1e3,1e4,1e5,1e6]){
    if(v>maxVal*1.05)break;
    const off=k*Math.sqrt(v);
    if(off<9||off>maxOff-1||off-last<11)continue;
    out.push({v,off});
    last=off;
  }
  return out.slice(-2);  // at most two labeled rungs per side
}

function niceTicks(t0,t1){
  const span=t1-t0;
  let step,fmt;
  if(span<=1.5*DAY){step=3*3600e3;fmt=ms=>new Date(ms).toLocaleTimeString(undefined,{hour:'numeric'});}
  else if(span<=4*DAY){step=12*3600e3;fmt=ms=>new Date(ms).toLocaleString(undefined,{weekday:'short',hour:'numeric'});}
  else if(span<=16*DAY){step=DAY;fmt=ms=>new Date(ms).toLocaleDateString(undefined,{month:'short',day:'numeric'});}
  else if(span<=50*DAY){step=7*DAY;fmt=ms=>new Date(ms).toLocaleDateString(undefined,{month:'short',day:'numeric'});}
  else if(span<=140*DAY){step=14*DAY;fmt=ms=>new Date(ms).toLocaleDateString(undefined,{month:'short',day:'numeric'});}
  else{step=30*DAY;fmt=ms=>new Date(ms).toLocaleDateString(undefined,{month:'short',year:'2-digit'});}
  const ticks=[];
  const start=Math.ceil(t0/step)*step;
  for(let ms=start;ms<t1-span*0.03&&ticks.length<8;ms+=step)ticks.push({ms,label:fmt(ms)});
  return ticks;
}

const f2=v=>v.toFixed(2);

/* Step-terrace envelope as an SVG path. `pos(ms)` maps time to the along-
   axis coordinate; `off(lines)` to the across-axis offset; `pt(a,o)`
   assembles the final (x,y) string for this orientation/side. Returns
   {edge, fill} path strings (fill closes back along the spine). */
function terracePath(items,t0ms,t1ms,pos,offOf,pt,cumKeyB,cumKeyA){
  const a0=pos(t0ms),a1=pos(t1ms);
  let edge=`M ${pt(a0,0)}`;
  let cur=0;
  for(const it of items){
    const a=pos(it.ms);
    edge+=` L ${pt(a,offOf(it[cumKeyB]))} L ${pt(a,offOf(it[cumKeyA]))}`;
    cur=it[cumKeyA];
  }
  edge+=` L ${pt(a1,offOf(cur))}`;
  const fill=edge+` L ${pt(a1,0)} Z`;
  return {edge,fill};
}

/* ============================ horizontal mini ============================ */

function renderMini(svg,W,H,grouped,opts){
  const g=grouped.groups[0];
  const t0=grouped.t0,t1=grouped.t1>grouped.t0?grouped.t1:grouped.t0+1;

  const ML=26,MR=8,MT=13,MB=19;
  const plotL=ML,plotR=Math.max(W-MR,ML+40);
  const plotTop=MT,plotBottom=H-MB;
  const xForMs=ms=>plotL+clamp((ms-t0)/((t1-t0)||1),0,1)*(plotR-plotL);

  const pg=prepGroup(g,Math.max(24,Math.floor((plotR-plotL)/3)));
  const sIns=Math.sqrt(Math.max(pg.totalIns,1)),sDel=Math.sqrt(Math.max(pg.totalDel,0));
  // Spine splits the vertical budget in proportion to each side's sqrt
  // total (clamped so neither side collapses); one k serves both sides.
  const extent=plotBottom-plotTop;
  const fIns=clamp(sIns/((sIns+sDel)||1),0.4,0.78);
  const spineY=plotTop+extent*fIns;
  const k=Math.min((spineY-plotTop)/sIns,sDel>0?(plotBottom-spineY)/sDel:1e9);
  const offOf=lines=>k*Math.sqrt(Math.max(lines,0));

  // gridlines + gutter labels at TRUE values on both banks
  const insTicks=sqrtTicks(k,pg.totalIns,spineY-plotTop);
  const delTicks=sqrtTicks(k,pg.totalDel,plotBottom-spineY);
  for(const tk of insTicks){
    const y=spineY-tk.off;
    svg.appendChild(ce('line',{x1:plotL,y1:f2(y),x2:plotR,y2:f2(y),stroke:opts.colorLine,'stroke-width':1}));
    const t=ce('text',{x:plotL-3,y:f2(y+2.5),'text-anchor':'end','font-size':6.6,fill:opts.colorInk3});
    t.textContent=fmtK(tk.v);svg.appendChild(t);
  }
  for(const tk of delTicks){
    const y=spineY+tk.off;
    svg.appendChild(ce('line',{x1:plotL,y1:f2(y),x2:plotR,y2:f2(y),stroke:opts.colorLine,'stroke-width':1}));
    const t=ce('text',{x:plotL-3,y:f2(y+2.5),'text-anchor':'end','font-size':6.6,fill:opts.colorInk3});
    t.textContent=fmtK(tk.v);svg.appendChild(t);
  }
  const z=ce('text',{x:plotL-3,y:f2(spineY+2.5),'text-anchor':'end','font-size':6.6,fill:opts.colorInk3});
  z.textContent='0';svg.appendChild(z);

  // envelopes (soft gradient fills fading toward the spine, per the mockup)
  const gidSafe=String(g.id||'g').replace(/[^a-z0-9_-]/gi,'_');
  const defs=ce('defs');svg.appendChild(defs);
  const gi=ce('linearGradient',{id:`stg-i-${gidSafe}`,x1:0,y1:f2(plotTop),x2:0,y2:f2(spineY),gradientUnits:'userSpaceOnUse'});
  gi.appendChild(ce('stop',{offset:'0','stop-color':opts.colorIns,'stop-opacity':0.26}));
  gi.appendChild(ce('stop',{offset:'1','stop-color':opts.colorIns,'stop-opacity':0.07}));
  defs.appendChild(gi);
  const gd=ce('linearGradient',{id:`stg-d-${gidSafe}`,x1:0,y1:f2(spineY),x2:0,y2:f2(plotBottom),gradientUnits:'userSpaceOnUse'});
  gd.appendChild(ce('stop',{offset:'0','stop-color':opts.colorDel,'stop-opacity':0.08}));
  gd.appendChild(ce('stop',{offset:'1','stop-color':opts.colorDel,'stop-opacity':0.28}));
  defs.appendChild(gd);

  const ptUp=(x,off)=>`${f2(x)},${f2(spineY-off)}`;
  const ptDn=(x,off)=>`${f2(x)},${f2(spineY+off)}`;
  const up=terracePath(pg.items,t0,t1,xForMs,offOf,ptUp,'insBefore','insAfter');
  const dn=terracePath(pg.items,t0,t1,xForMs,offOf,ptDn,'delBefore','delAfter');
  svg.appendChild(ce('path',{d:up.fill,fill:`url(#stg-i-${gidSafe})`}));
  svg.appendChild(ce('path',{d:dn.fill,fill:`url(#stg-d-${gidSafe})`}));
  svg.appendChild(ce('path',{d:up.edge,fill:'none',stroke:opts.colorIns,'stroke-width':1.4,'stroke-opacity':0.85,'stroke-linejoin':'round'}));
  svg.appendChild(ce('path',{d:dn.edge,fill:'none',stroke:opts.colorDel,'stroke-width':1.4,'stroke-opacity':0.85,'stroke-linejoin':'round'}));
  svg.appendChild(ce('line',{x1:plotL,y1:f2(spineY),x2:plotR,y2:f2(spineY),stroke:opts.colorInk3,'stroke-width':1,'stroke-opacity':0.7}));

  // per-commit risers: bold the visible steps, and give EVERY item an
  // invisible hit target on both edges so tiny commits stay hoverable
  for(const it of pg.items){
    const x=xForMs(it.ms);
    const yi0=spineY-offOf(it.insBefore),yi1=spineY-offOf(it.insAfter);
    const yd0=spineY+offOf(it.delBefore),yd1=spineY+offOf(it.delAfter);
    if(yi0-yi1>0.6)svg.appendChild(ce('line',{x1:f2(x),y1:f2(yi0),x2:f2(x),y2:f2(yi1),stroke:opts.colorIns,'stroke-width':2.2,'stroke-linecap':'round'}));
    if(yd1-yd0>0.6)svg.appendChild(ce('line',{x1:f2(x),y1:f2(yd0),x2:f2(x),y2:f2(yd1),stroke:opts.colorDel,'stroke-width':2.2,'stroke-linecap':'round'}));
    const hitI=ce('circle',{cx:f2(x),cy:f2(yi1),r:5,fill:'transparent'});
    attachHover(hitI,it.ev,opts);svg.appendChild(hitI);
    if(num0(it.ev.deletions)>0){
      const hitD=ce('circle',{cx:f2(x),cy:f2(yd1),r:5,fill:'transparent'});
      attachHover(hitD,it.ev,opts);svg.appendChild(hitD);
    }
  }

  // selective direct label: the single largest event (ins+del)
  let big=null;
  for(const it of pg.items){
    const m=num0(it.ev.insertions)+num0(it.ev.deletions);
    if(!big||m>big.m)big={it,m};
  }
  if(big&&big.m>0){
    const it=big.it,x=xForMs(it.ms);
    const insSide=num0(it.ev.insertions)>=num0(it.ev.deletions);
    const y=insSide?spineY-offOf(it.insAfter)-4:spineY+offOf(it.delAfter)+9;
    const lbl=ce('text',{x:f2(clamp(x+4,plotL,plotR-60)),y:f2(clamp(y,plotTop+7,plotBottom-2)),'font-size':6.8,fill:opts.colorInk});
    lbl.setAttribute('fill-opacity','0.85');
    lbl.textContent=(insSide?'+':'-')+fmtK(insSide?it.ev.insertions:it.ev.deletions);
    svg.appendChild(lbl);
  }

  // endpoint totals — the exact-number anchors for both banks
  const eIns=ce('text',{x:f2(plotR),y:f2(clamp(spineY-offOf(pg.totalIns)-3,plotTop+7,spineY-2)),'text-anchor':'end','font-size':7,fill:opts.colorIns});
  eIns.textContent='+'+fmtK(pg.totalIns);svg.appendChild(eIns);
  if(pg.totalDel>0){
    const eDel=ce('text',{x:f2(plotR),y:f2(clamp(spineY+offOf(pg.totalDel)+8,spineY+8,plotBottom+9)),'text-anchor':'end','font-size':7,fill:opts.colorDel});
    eDel.textContent='-'+fmtK(pg.totalDel);svg.appendChild(eDel);
  }

  // worktree shoots: lime arcs off the insertion edge, capped at two
  const wts=[...pg.byWt.entries()].sort((a,b)=>b[1].length-a[1].length).slice(0,2);
  wts.forEach(([name,evs],wi)=>{
    const ms=evs.map(e=>msOf(e.date));
    const forkMs=ms[0],lastMs=ms[ms.length-1];
    const stillOpen=(t1-lastMs)<=(t1-t0)*0.06;
    const x0=xForMs(forkMs),x1=xForMs(stillOpen?t1:lastMs);
    const yE0=spineY-offOf(cumAt(pg.items,forkMs,'insAfter'));
    const yE1=spineY-offOf(cumAt(pg.items,stillOpen?t1:lastMs,'insAfter'));
    const apex=Math.max(plotTop+3+wi*7,Math.min(yE0,yE1)-11);
    const d=stillOpen
      ?`M ${f2(x0)},${f2(yE0)} C ${f2(x0+8)},${f2(apex+2)} ${f2((x0+x1)/2)},${f2(apex)} ${f2(x1)},${f2(apex)}`
      :`M ${f2(x0)},${f2(yE0)} C ${f2(x0+8)},${f2(apex+2)} ${f2(x1-8)},${f2(apex+2)} ${f2(x1)},${f2(yE1)}`;
    svg.appendChild(ce('path',{d,fill:'none',stroke:opts.colorAccent,'stroke-width':1.3,'stroke-opacity':0.55}));
    if(stillOpen)svg.appendChild(ce('circle',{cx:f2(x1),cy:f2(apex),r:1.6,fill:opts.colorAccent}));
    // paired per-commit ticks on the arc: sqrt-of-magnitude accents (the
    // envelopes own the ruler; shoot ticks are context, popover is exact)
    evs.forEach((e,i)=>{
      const x=xForMs(ms[i]);
      const frac=clamp((x-x0)/((x1-x0)||1),0,1);
      const yArc=stillOpen?apex+(yE0-apex)*Math.pow(1-frac,2):apex+2+(Math.abs(yE0-apex-2))*Math.pow(Math.abs(frac-0.5)*2,2);
      const li=Math.min(k*Math.sqrt(num0(e.insertions)),10),ld=Math.min(k*Math.sqrt(num0(e.deletions)),10);
      if(li>0.5)svg.appendChild(ce('line',{x1:f2(x),y1:f2(yArc),x2:f2(x),y2:f2(yArc-li),stroke:opts.colorIns,'stroke-width':1.6,'stroke-linecap':'round'}));
      if(ld>0.5)svg.appendChild(ce('line',{x1:f2(x),y1:f2(yArc),x2:f2(x),y2:f2(yArc+ld),stroke:opts.colorDel,'stroke-width':1.6,'stroke-linecap':'round'}));
      const hit=ce('circle',{cx:f2(x),cy:f2(yArc),r:5,fill:'transparent'});
      attachHover(hit,e,opts);svg.appendChild(hit);
    });
    const lx=clamp((x0+x1)/2,plotL+14,plotR-14);
    const nameEl=ce('text',{x:f2(lx),y:f2(Math.max(plotTop+6,apex-3)),'text-anchor':'middle','font-size':6.4,'font-style':'italic',fill:opts.colorAccent});
    nameEl.setAttribute('fill-opacity','0.9');
    nameEl.textContent=name+(stillOpen?' · open':'');
    svg.appendChild(nameEl);
  });

  // chrome: caption, legend, time axis, root caption
  const cap=ce('text',{x:plotL,y:9,'font-size':6.8,fill:opts.colorInk3});
  cap.textContent='lines · √ scale';svg.appendChild(cap);
  const lgX=Math.max(plotL+70,W-72);
  svg.appendChild(ce('rect',{x:lgX,y:4,width:5,height:5,fill:opts.colorIns}));
  const li=ce('text',{x:lgX+8,y:9,'font-size':6.8,fill:opts.colorInk3});li.textContent='ins';svg.appendChild(li);
  svg.appendChild(ce('rect',{x:lgX+30,y:4,width:5,height:5,fill:opts.colorDel}));
  const ld=ce('text',{x:lgX+38,y:9,'font-size':6.8,fill:opts.colorInk3});ld.textContent='del';svg.appendChild(ld);

  for(const tk of niceTicks(t0,t1).slice(0,5)){
    const x=xForMs(tk.ms);
    if(x<plotL+26||x>plotR-24)continue;
    svg.appendChild(ce('line',{x1:f2(x),y1:f2(plotBottom+1),x2:f2(x),y2:f2(plotBottom+4),stroke:opts.colorLine,'stroke-width':1}));
    const t=ce('text',{x:f2(x),y:f2(plotBottom+11),'text-anchor':'middle','font-size':6.6,fill:opts.colorInk3});
    t.textContent=tk.label;svg.appendChild(t);
  }
  const rc=ce('text',{x:plotL,y:f2(plotBottom+11),'font-size':6.6,fill:opts.colorInk3});
  rc.textContent='root · '+(opts.fmtDate?opts.fmtDate(new Date(t0).toISOString()):'');
  svg.appendChild(rc);
  const nowT=ce('text',{x:f2(plotR),y:f2(plotBottom+11),'text-anchor':'end','font-size':6.6,fill:opts.colorInk3});
  nowT.textContent='now';svg.appendChild(nowT);
}

/* =========================== vertical full view ========================== */

function drawTimeAxis(svg,ML,MR,W,plotTop,plotBottom,t0,t1,opts){
  const yForMs=ms=>plotBottom-clamp((ms-t0)/((t1-t0)||1),0,1)*(plotBottom-plotTop);
  for(const tk of niceTicks(t0,t1)){
    const y=yForMs(tk.ms);
    svg.appendChild(ce('line',{x1:ML,y1:f2(y),x2:W-MR,y2:f2(y),stroke:opts.colorLine,'stroke-width':1}));
    const txt=ce('text',{x:ML-6,y:f2(y+3),'text-anchor':'end','font-size':8.5,fill:opts.colorInk3});
    txt.textContent=tk.label;
    svg.appendChild(txt);
  }
  const nowTxt=ce('text',{x:ML-6,y:f2(plotTop+3),'text-anchor':'end','font-size':8.5,fill:opts.colorInk3});
  nowTxt.textContent='now';
  svg.appendChild(nowTxt);
}

/* One shared sqrt ruler, drawn once bottom-right: 0 at the spine mark then
   rungs at true values so any trunk's half-width decodes to lines. */
function drawRuler(svg,W,H,k,maxVal,maxOff,opts){
  const ticks=sqrtTicks(k,maxVal,maxOff);
  if(!ticks.length)return;
  const len=ticks[ticks.length-1].off;
  const x0=W-14-len,y=H-12;
  svg.appendChild(ce('line',{x1:f2(x0),y1:y,x2:f2(x0+len),y2:y,stroke:opts.colorInk3,'stroke-width':1,'stroke-opacity':0.6}));
  svg.appendChild(ce('line',{x1:f2(x0),y1:y-3,x2:f2(x0),y2:y+3,stroke:opts.colorInk3,'stroke-width':1,'stroke-opacity':0.6}));
  const z=ce('text',{x:f2(x0),y:y+11,'text-anchor':'middle','font-size':7,fill:opts.colorInk3});
  z.textContent='0';svg.appendChild(z);
  for(const tk of ticks){
    svg.appendChild(ce('line',{x1:f2(x0+tk.off),y1:y-3,x2:f2(x0+tk.off),y2:y+3,stroke:opts.colorInk3,'stroke-width':1,'stroke-opacity':0.6}));
    const t=ce('text',{x:f2(x0+tk.off),y:y+11,'text-anchor':'middle','font-size':7,fill:opts.colorInk3});
    t.textContent=fmtK(tk.v);svg.appendChild(t);
  }
  const cap=ce('text',{x:f2(x0-6),y:y+3,'text-anchor':'end','font-size':7,fill:opts.colorInk3});
  cap.textContent='lines · √';
  svg.appendChild(cap);
}

function renderFull(svg,W,H,grouped,opts){
  const groups=grouped.groups||[];
  const t0=grouped.t0,t1=grouped.t1>grouped.t0?grouped.t1:grouped.t0+1;

  const ML=64,MR=18,MT=46,MB=30;
  const plotTop=MT,plotBottom=Math.max(MT+40,H-MB);
  const yForMs=ms=>plotBottom-clamp((ms-t0)/((t1-t0)||1),0,1)*(plotBottom-plotTop);

  drawTimeAxis(svg,ML,MR,W,plotTop,plotBottom,t0,t1,opts);

  const n=groups.length;
  const innerW=Math.max(W-ML-MR,40);
  const colW=innerW/n;
  const target=Math.max(30,Math.floor((plotBottom-plotTop)/3));

  const perGroup=groups.map(g=>prepGroup(g,target));
  let maxSide=1;
  for(const pg of perGroup)maxSide=Math.max(maxSide,pg.totalIns,pg.totalDel);
  const colCap=Math.min(60,Math.max(10,colW*0.4));
  const k=colCap/Math.sqrt(maxSide);
  const offOf=lines=>k*Math.sqrt(Math.max(lines,0));

  const defs=ce('defs');svg.appendChild(defs);

  perGroup.forEach((pg,gi)=>{
    const cx=ML+colW*(gi+0.5);
    const ptR=(y,off)=>`${f2(cx+off)},${f2(y)}`;   // insertions grow right
    const ptL=(y,off)=>`${f2(cx-off)},${f2(y)}`;   // deletions grow left
    const up=terracePath(pg.items,t0,t1,yForMs,offOf,ptR,'insBefore','insAfter');
    const dn=terracePath(pg.items,t0,t1,yForMs,offOf,ptL,'delBefore','delAfter');
    svg.appendChild(ce('path',{d:up.fill,fill:opts.hexA(opts.colorIns,0.13)}));
    svg.appendChild(ce('path',{d:dn.fill,fill:opts.hexA(opts.colorDel,0.13)}));
    svg.appendChild(ce('path',{d:up.edge,fill:'none',stroke:opts.colorIns,'stroke-width':1.4,'stroke-opacity':0.8,'stroke-linejoin':'round'}));
    svg.appendChild(ce('path',{d:dn.edge,fill:'none',stroke:opts.colorDel,'stroke-width':1.4,'stroke-opacity':0.8,'stroke-linejoin':'round'}));
    svg.appendChild(ce('line',{x1:f2(cx),y1:f2(plotBottom),x2:f2(cx),y2:f2(plotTop),stroke:opts.colorInk3,'stroke-width':1,'stroke-opacity':0.55}));

    for(const it of pg.items){
      const y=yForMs(it.ms);
      const xi0=cx+offOf(it.insBefore),xi1=cx+offOf(it.insAfter);
      const xd0=cx-offOf(it.delBefore),xd1=cx-offOf(it.delAfter);
      if(xi1-xi0>0.6)svg.appendChild(ce('line',{x1:f2(xi0),y1:f2(y),x2:f2(xi1),y2:f2(y),stroke:opts.colorIns,'stroke-width':2.2,'stroke-linecap':'round'}));
      if(xd0-xd1>0.6)svg.appendChild(ce('line',{x1:f2(xd0),y1:f2(y),x2:f2(xd1),y2:f2(y),stroke:opts.colorDel,'stroke-width':2.2,'stroke-linecap':'round'}));
      const hitI=ce('circle',{cx:f2(xi1),cy:f2(y),r:5.5,fill:'transparent'});
      attachHover(hitI,it.ev,opts);svg.appendChild(hitI);
      if(num0(it.ev.deletions)>0){
        const hitD=ce('circle',{cx:f2(xd1),cy:f2(y),r:5.5,fill:'transparent'});
        attachHover(hitD,it.ev,opts);svg.appendChild(hitD);
      }
    }

    // bud + name + exact totals for both banks
    const budY=plotTop;
    const gid=`stbud-${gi}`;
    const rg=ce('radialGradient',{id:gid,cx:'50%',cy:'50%',r:'50%'});
    rg.appendChild(ce('stop',{offset:'0%','stop-color':pg.g.color,'stop-opacity':0.55}));
    rg.appendChild(ce('stop',{offset:'100%','stop-color':pg.g.color,'stop-opacity':0}));
    defs.appendChild(rg);
    svg.appendChild(ce('circle',{cx:f2(cx),cy:budY,r:10,fill:`url(#${gid})`}));
    const dot=ce('circle',{cx:f2(cx),cy:budY,r:2.4,fill:pg.g.color});
    dot.style.stroke='var(--bg)';dot.style.strokeWidth='1';
    svg.appendChild(dot);
    const nameEl=ce('text',{x:f2(cx),y:budY-24,'text-anchor':'middle','font-size':11,'font-weight':600,fill:opts.colorInk});
    nameEl.textContent=pg.g.label;
    svg.appendChild(nameEl);
    const sumEl=ce('text',{x:f2(cx),y:budY-13,'text-anchor':'middle','font-size':8,fill:opts.colorInk3});
    const tIns=document.createElementNS(SVGNS,'tspan');tIns.setAttribute('fill',opts.colorIns);tIns.textContent='+'+fmtK(pg.totalIns);
    const tMid=document.createElementNS(SVGNS,'tspan');tMid.textContent=' · ';
    const tDel=document.createElementNS(SVGNS,'tspan');tDel.setAttribute('fill',opts.colorDel);tDel.textContent='-'+fmtK(pg.totalDel);
    sumEl.appendChild(tIns);sumEl.appendChild(tMid);sumEl.appendChild(tDel);
    svg.appendChild(sumEl);

    // worktree shoots off the insertion edge (right), capped at three
    const wts=[...pg.byWt.entries()].sort((a,b)=>b[1].length-a[1].length).slice(0,3);
    const clampX=x=>clamp(x,ML+2,W-MR-2);
    wts.forEach(([name,evs],wi)=>{
      const ms=evs.map(e=>msOf(e.date));
      const forkMs=ms[0],lastMs=ms[ms.length-1];
      const stillOpen=(t1-lastMs)<=(t1-t0)*0.06;
      const y0=yForMs(forkMs),y1=yForMs(stillOpen?t1:lastMs);
      const xE0=cx+offOf(cumAt(pg.items,forkMs,'insAfter'));
      const xE1=cx+offOf(cumAt(pg.items,stillOpen?t1:lastMs,'insAfter'));
      const apex=clampX(Math.max(xE0,xE1)+9+wi*7);
      const d=stillOpen
        ?`M ${f2(xE0)},${f2(y0)} C ${f2(apex-2)},${f2(y0-6)} ${f2(apex)},${f2((y0+y1)/2)} ${f2(apex)},${f2(y1)}`
        :`M ${f2(xE0)},${f2(y0)} C ${f2(apex)},${f2(y0-5)} ${f2(apex)},${f2(y1+5)} ${f2(xE1)},${f2(y1)}`;
      svg.appendChild(ce('path',{d,fill:'none',stroke:opts.colorAccent,'stroke-width':1.2,'stroke-opacity':0.55}));
      if(stillOpen)svg.appendChild(ce('circle',{cx:f2(apex),cy:f2(y1),r:1.7,fill:opts.colorAccent}));
      evs.forEach((e,i)=>{
        const y=yForMs(ms[i]);
        const frac=clamp((y0-y)/((y0-y1)||1),0,1);
        const xArc=stillOpen?xE0+(apex-xE0)*Math.min(frac*1.6,1):xE0+(apex-xE0)*Math.sin(Math.PI*frac);
        const li=Math.min(k*Math.sqrt(num0(e.insertions)),10),ld=Math.min(k*Math.sqrt(num0(e.deletions)),10);
        if(li>0.5)svg.appendChild(ce('line',{x1:f2(clampX(xArc)),y1:f2(y),x2:f2(clampX(xArc+li)),y2:f2(y),stroke:opts.colorIns,'stroke-width':1.6,'stroke-linecap':'round'}));
        if(ld>0.5)svg.appendChild(ce('line',{x1:f2(clampX(xArc)),y1:f2(y),x2:f2(clampX(xArc-ld)),y2:f2(y),stroke:opts.colorDel,'stroke-width':1.6,'stroke-linecap':'round'}));
        const hit=ce('circle',{cx:f2(clampX(xArc)),cy:f2(y),r:5,fill:'transparent'});
        attachHover(hit,e,opts);svg.appendChild(hit);
      });
      const nameEl2=ce('text',{x:f2(clampX(apex+3)),y:f2((y0+y1)/2-4),'font-size':7.2,'font-style':'italic',fill:opts.colorInk3});
      nameEl2.textContent=name;
      svg.appendChild(nameEl2);
      const st=ce('text',{x:f2(clampX(apex+3)),y:f2((y0+y1)/2+5),'font-size':6.4,fill:opts.colorAccent});
      st.textContent=stillOpen?'still open':'merges back';
      svg.appendChild(st);
    });
  });

  // legend (top-right) + shared ruler (bottom-right) + root caption
  const lgX=Math.max(4,W-96);
  svg.appendChild(ce('rect',{x:lgX,y:7,width:6,height:6,fill:opts.colorIns}));
  const t1e=ce('text',{x:lgX+10,y:13,'font-size':8,fill:opts.colorInk3});t1e.textContent='+ins';svg.appendChild(t1e);
  svg.appendChild(ce('rect',{x:lgX+40,y:7,width:6,height:6,fill:opts.colorDel}));
  const t2e=ce('text',{x:lgX+50,y:13,'font-size':8,fill:opts.colorInk3});t2e.textContent='-del';svg.appendChild(t2e);
  drawRuler(svg,W,H,k,maxSide,colCap+8,opts);
  svg.appendChild(ce('line',{x1:ML,y1:plotBottom,x2:ML,y2:Math.min(plotBottom+6,H-1),stroke:opts.colorLine,'stroke-width':1}));
  const rc=ce('text',{x:ML,y:Math.min(plotBottom+16,H-3),'text-anchor':'start','font-size':8,fill:opts.colorInk3});
  rc.textContent=`root · ${opts.fmtDate?opts.fmtDate(new Date(t0).toISOString()):''}`;
  svg.appendChild(rc);
}

export function renderSplitTrunk(svg,W,H,grouped,opts){
  const groups=grouped.groups||[];
  if(!groups.length)return;
  if(opts.orient==='h')renderMini(svg,W,H,grouped,opts);
  else renderFull(svg,W,H,grouped,opts);
}
