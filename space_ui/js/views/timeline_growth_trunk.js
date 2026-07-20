/* Growth-trunk renderer for the Environments space. One tapered vertical
   stem per cluster, rooted at t0 (bottom) and capped by a glowing "bud" at
   t1/now (top). Stem half-width tracks the running sum of insertions minus
   deletions for every non-worktree event in that group up to the sampled
   instant, sqrt-compressed so a 17k-line trunk and a 2k-line trunk both
   read on one canvas. Worktree commits fork into thin side "leaf" shoots
   with their own much-smaller cumulative sum instead of feeding the trunk.
   Pure function of (svg, W, H, grouped, opts) — no imports, no module
   state; every hoverable glyph routes through opts.onHover/onMove/onLeave. */

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

function fmtNet(n){
  const sign=n<0?'-':'+',a=Math.abs(n);
  const body=a>=1e6?(a/1e6).toFixed(1)+'M':a>=1e3?(a/1e3).toFixed(1)+'K':String(Math.round(a));
  return sign+body;
}

// Smooth an open/closed point list into a path 'd' via quadratic curves
// through inter-point midpoints — no matrix solving, degrades gracefully
// for 2-3 point lists (the n=1-event case), still a valid path either way.
function smoothPath(pts,close){
  if(!pts.length)return '';
  const f=v=>v.toFixed(2);
  if(pts.length===1)return `M ${f(pts[0][0])},${f(pts[0][1])}`;
  let d=`M ${f(pts[0][0])},${f(pts[0][1])}`;
  for(let i=1;i<pts.length-1;i++){
    const mx=(pts[i][0]+pts[i+1][0])/2,my=(pts[i][1]+pts[i+1][1])/2;
    d+=` Q ${f(pts[i][0])},${f(pts[i][1])} ${f(mx)},${f(my)}`;
  }
  const last=pts[pts.length-1];
  d+=` L ${f(last[0])},${f(last[1])}`;
  if(close)d+=' Z';
  return d;
}

// Linear-interpolate a group's trunk half-width at an arbitrary y, so
// worktree forks/rejoins and per-event bumps sit exactly on the drawn
// silhouette instead of a separately-recomputed (and possibly mismatched)
// value. sampleY is monotonically decreasing (index0 = t0/bottom).
function hwAt(sampleY,hw,y){
  const n=sampleY.length;
  if(!n)return 0;
  if(y>=sampleY[0])return hw[0];
  if(y<=sampleY[n-1])return hw[n-1];
  for(let i=0;i<n-1;i++){
    const y0=sampleY[i],y1=sampleY[i+1];
    if(y<=y0+1e-6&&y>=y1-1e-6){
      const f=(y0-y)/((y0-y1)||1);
      return hw[i]+(hw[i+1]-hw[i])*f;
    }
  }
  return hw[n-1];
}

function attachHover(el,ev,opts){
  el.style.cursor='pointer';
  el.addEventListener('pointerenter',e=>opts.onHover(ev,e.clientX,e.clientY));
  el.addEventListener('pointermove',e=>opts.onMove(e.clientX,e.clientY));
  el.addEventListener('pointerleave',()=>opts.onLeave());
}

// Density guard: a cluster can carry 1000+ commits. Above `target` we
// coalesce contiguous (by index, already date-sorted) slices into one
// synthesized summary event per bucket, matching the {title, insertions,
// deletions, files:[], files_count, date, project_label, worktree:null,
// author, sha} shape the popover reads — bucket position/date is the
// slice's first event, so the dot's y matches the label exactly.
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

function magR(ins,del){return clamp(2+1.15*Math.sqrt(num0(ins)+num0(del)),2,7.5);}

function drawBump(svg,x,y,ev,opts){
  const isIns=num0(ev.insertions)>=num0(ev.deletions);
  const c=ce('circle',{cx:x.toFixed(2),cy:y.toFixed(2),r:magR(ev.insertions,ev.deletions).toFixed(2),
    fill:isIns?opts.colorIns:opts.colorDel,'fill-opacity':0.92});
  c.style.stroke='var(--bg)';c.style.strokeWidth='1';
  attachHover(c,ev,opts);
  svg.appendChild(c);
}

function drawTrunk(svg,cx,sampleY,hw,g,opts){
  const left=sampleY.map((y,i)=>[cx-hw[i],y]);
  const right=sampleY.map((y,i)=>[cx+hw[i],y]).reverse();
  const d=smoothPath(left.concat(right),true);
  svg.appendChild(ce('path',{d,fill:opts.hexA(g.color,0.14),stroke:g.color,
    'stroke-opacity':0.7,'stroke-width':1.3,'stroke-linejoin':'round'}));
}

function drawMainBumps(svg,cx,hwAtY,yForMs,pg,opts){
  const {main,mainMs,g}=pg;
  if(!main.length)return;
  const items=main.length<=140?main.map((e,i)=>({ev:e,ms:mainMs[i]})):bucketEvents(main,mainMs,90,g.label);
  for(const {ev,ms} of items){
    const y=yForMs(ms);
    drawBump(svg,cx+hwAtY(y),y,ev,opts);
  }
}

function drawBudAndLabel(svg,defs,cx,budY,g,totalNet,gi,opts){
  const gid=`budglow-${gi}`;
  const rg=ce('radialGradient',{id:gid,cx:'50%',cy:'50%',r:'50%'});
  rg.appendChild(ce('stop',{offset:'0%','stop-color':g.color,'stop-opacity':0.55}));
  rg.appendChild(ce('stop',{offset:'100%','stop-color':g.color,'stop-opacity':0}));
  defs.appendChild(rg);
  svg.appendChild(ce('circle',{cx,cy:budY,r:10,fill:`url(#${gid})`}));
  const dot=ce('circle',{cx,cy:budY,r:2.4,fill:g.color});
  dot.style.stroke='var(--bg)';dot.style.strokeWidth='1';
  svg.appendChild(dot);

  const nameEl=ce('text',{x:cx,y:budY-24,'text-anchor':'middle','font-size':11,'font-weight':600,fill:opts.colorInk});
  nameEl.textContent=g.label;
  svg.appendChild(nameEl);

  const sumEl=ce('text',{x:cx,y:budY-13,'text-anchor':'middle','font-size':8,fill:opts.colorInk3});
  sumEl.textContent=`${fmtNet(totalNet)} net lines`;
  svg.appendChild(sumEl);
}

function drawWorktree(svg,cx,W,wtName,wtEvents,sampleY,hw,wtIndex,t0,t1,plotTop,plotBottom,opts){
  const ms=wtEvents.map(e=>msOf(e.date));
  const forkMs=ms[0],lastMs=ms[ms.length-1];
  const yForMs=m=>plotBottom-clamp((m-t0)/((t1-t0)||1),0,1)*(plotBottom-plotTop);
  const hwAtY=y=>hwAt(sampleY,hw,y);
  const baseSide=cx<W/2?1:-1;
  const side=wtIndex%2===0?baseSide:-baseSide;
  const clampX=x=>clamp(x,3,W-3);

  // No merge signal in the data, only timestamps — treat a worktree whose
  // last commit lands in the top slice of the visible window as still
  // active, and cap it in open air instead of tapering back to the trunk.
  const stillOpen=(t1-lastMs)<=(t1-t0)*0.06;

  if(ms.length===1){
    const y=yForMs(forkMs),ax=clampX(cx+side*hwAtY(y)),tip=clampX(ax+side*7);
    const d=`M ${ax.toFixed(2)},${(y-3).toFixed(2)} Q ${tip.toFixed(2)},${y.toFixed(2)} ${ax.toFixed(2)},${(y+3).toFixed(2)} Q ${ax.toFixed(2)},${y.toFixed(2)} ${ax.toFixed(2)},${(y-3).toFixed(2)} Z`;
    svg.appendChild(ce('path',{d,fill:opts.hexA(opts.colorAccent,0.16),stroke:opts.colorAccent,'stroke-opacity':0.55,'stroke-width':1}));
    drawWtLabel(svg,clampX(ax+side*10),y,wtName,stillOpen,side,opts);
    drawBump(svg,clampX(ax+side*3),y,wtEvents[0],opts);
    return;
  }

  const LN=Math.max(6,Math.min(18,ms.length));
  const localMs=new Array(LN+1);
  for(let i=0;i<=LN;i++)localMs[i]=forkMs+(lastMs-forkMs)*i/LN;
  let idx=0,run=0;
  const cum=new Array(LN+1);
  for(let i=0;i<=LN;i++){
    while(idx<ms.length&&ms[idx]<=localMs[i]){run+=num0(wtEvents[idx].insertions)-num0(wtEvents[idx].deletions);idx++;}
    cum[i]=run;
  }
  const finalCum=Math.max(run,1);
  const MAXW=9,k2=(MAXW-0.6)/Math.sqrt(finalCum);
  const innerPts=[],outerPts=[];
  for(let i=0;i<=LN;i++){
    const y=yForMs(localMs[i]),ax=cx+side*hwAtY(y),frac=i/LN;
    const taper=stillOpen?Math.sin(Math.PI*0.5*Math.min(frac*1.15,1)):Math.sin(Math.PI*frac);
    const w=Math.max(0,k2*Math.sqrt(Math.max(cum[i],0)))*Math.max(taper,0);
    innerPts.push([clampX(ax),y]);
    outerPts.push([clampX(ax+side*(w+0.5)),y]);
  }
  const pts=innerPts.concat(outerPts.slice().reverse());
  svg.appendChild(ce('path',{d:smoothPath(pts,true),fill:opts.hexA(opts.colorAccent,0.12),
    stroke:opts.colorAccent,'stroke-opacity':0.55,'stroke-width':1}));

  if(stillOpen){
    const tip=outerPts[outerPts.length-1];
    const tipDot=ce('circle',{cx:tip[0],cy:tip[1],r:1.8,fill:opts.colorAccent});
    svg.appendChild(tipDot);
  }

  const items=ms.length<=40?wtEvents.map((e,i)=>({ev:e,ms:ms[i]})):bucketEvents(wtEvents,ms,20,wtName);
  for(const {ev,ms:m} of items){
    const y=yForMs(m),ax=cx+side*hwAtY(y);
    drawBump(svg,clampX(ax+side*3),y,ev,opts);
  }
  drawWtLabel(svg,clampX(cx+side*(MAXW+7)),(yForMs(forkMs)+yForMs(lastMs))/2,wtName,stillOpen,side,opts);
}

function drawWtLabel(svg,x,y,name,stillOpen,side,opts){
  const anchor=side>0?'start':'end';
  const nameEl=ce('text',{x,y:y-4,'text-anchor':anchor,'font-size':7.2,'font-style':'italic',fill:opts.colorInk3});
  nameEl.textContent=name;
  svg.appendChild(nameEl);
  const statusEl=ce('text',{x,y:y+5,'text-anchor':anchor,'font-size':6.4,fill:opts.colorAccent});
  statusEl.textContent=stillOpen?'still open':'merges back';
  svg.appendChild(statusEl);
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

function drawTimeAxis(svg,ML,MR,W,plotTop,plotBottom,t0,t1,opts){
  const yForMs=ms=>plotBottom-clamp((ms-t0)/((t1-t0)||1),0,1)*(plotBottom-plotTop);
  for(const tk of niceTicks(t0,t1)){
    const y=yForMs(tk.ms);
    svg.appendChild(ce('line',{x1:ML,y1:y.toFixed(2),x2:W-MR,y2:y.toFixed(2),stroke:opts.colorLine,'stroke-width':1}));
    const txt=ce('text',{x:ML-6,y:(y+3).toFixed(2),'text-anchor':'end','font-size':8.5,fill:opts.colorInk3});
    txt.textContent=tk.label;
    svg.appendChild(txt);
  }
  const nowTxt=ce('text',{x:ML-6,y:(plotTop+3).toFixed(2),'text-anchor':'end','font-size':8.5,fill:opts.colorInk3});
  nowTxt.textContent='now';
  svg.appendChild(nowTxt);
}

function drawLegend(svg,W,opts){
  const y=10,x=Math.max(4,W-90);
  const c1=ce('circle',{cx:x,cy:y,r:3,fill:opts.colorIns});svg.appendChild(c1);
  const t1e=ce('text',{x:x+6,y:y+3,'font-size':8,fill:opts.colorInk3});t1e.textContent='ins';svg.appendChild(t1e);
  const c2=ce('circle',{cx:x+34,cy:y,r:3,fill:opts.colorDel});svg.appendChild(c2);
  const t2e=ce('text',{x:x+40,y:y+3,'font-size':8,fill:opts.colorInk3});t2e.textContent='del';svg.appendChild(t2e);
}

function drawRootCaption(svg,ML,plotBottom,t0,H,opts){
  svg.appendChild(ce('line',{x1:ML,y1:plotBottom,x2:ML,y2:Math.min(plotBottom+6,H-1),stroke:opts.colorLine,'stroke-width':1}));
  const txt=ce('text',{x:ML,y:Math.min(plotBottom+16,H-3),'text-anchor':'start','font-size':8,fill:opts.colorInk3});
  txt.textContent=`root · ${opts.fmtDate?opts.fmtDate(new Date(t0).toISOString()):''}`;
  svg.appendChild(txt);
}

export function renderGrowthTrunk(svg,W,H,grouped,opts){
  const groups=grouped.groups||[];
  if(!groups.length)return;
  const t0=grouped.t0,t1=grouped.t1>grouped.t0?grouped.t1:grouped.t0+1;

  const ML=18,MR=18,MT=46;
  const MB=28;
  const plotTop=MT,plotBottom=Math.max(MT+40,H-MB),plotH=plotBottom-plotTop;
  const yForMs=ms=>plotBottom-clamp((ms-t0)/((t1-t0)||1),0,1)*plotH;

  const defs=ce('defs');
  svg.appendChild(defs);
  drawTimeAxis(svg,ML,MR,W,plotTop,plotBottom,t0,t1,opts);
  drawLegend(svg,W,opts);

  const n=groups.length;
  const innerW=Math.max(W-ML-MR,40);
  const colW=innerW/n;

  const STEPS=64,MIN_HW=3,BUD_PINCH=1.4,ROOT_PINCH=2.2;
  const tz=Math.max(3,Math.round(STEPS*0.06));
  const sampleMs=new Array(STEPS+1);
  for(let s=0;s<=STEPS;s++)sampleMs[s]=t0+(t1-t0)*s/STEPS;
  const sampleY=sampleMs.map(yForMs);

  // One pass to build each group's main-trunk cumulative before picking a
  // shared sqrt scale, so a small cluster (Ops) and a large one (App) read
  // relative to the same yardstick instead of each self-normalizing.
  const perGroup=groups.map(g=>{
    const main=[],byWt=new Map();
    for(const e of (g.events||[])){
      if(e.worktree){
        if(!byWt.has(e.worktree))byWt.set(e.worktree,[]);
        byWt.get(e.worktree).push(e);
      }else main.push(e);
    }
    const mainMs=main.map(e=>msOf(e.date));
    const cum=new Array(STEPS+1);
    let idx=0,run=0;
    for(let s=0;s<=STEPS;s++){
      while(idx<main.length&&mainMs[idx]<=sampleMs[s]){run+=num0(main[idx].insertions)-num0(main[idx].deletions);idx++;}
      cum[s]=run;
    }
    return {g,main,mainMs,byWt,cum,total:run};
  });

  let globalMaxCum=0;
  for(const pg of perGroup)for(const c of pg.cum)if(c>globalMaxCum)globalMaxCum=c;

  const colCap=Math.min(46,Math.max(MIN_HW+4,colW*0.32));
  const k=globalMaxCum>0?(colCap-MIN_HW)/Math.sqrt(globalMaxCum):0;

  perGroup.forEach((pg,gi)=>{
    const cx=ML+colW*(gi+0.5);
    const hw=pg.cum.map((c,s)=>{
      let w=MIN_HW+k*Math.sqrt(Math.max(c,0));
      if(s<tz){const f=1-s/tz;w=w*(1-f)+ROOT_PINCH*f;}
      if(s>STEPS-tz){const f=(s-(STEPS-tz))/tz;w=w*(1-f)+BUD_PINCH*f;}
      return clamp(w,1.1,colCap+2);
    });
    const hwAtY=y=>hwAt(sampleY,hw,y);

    drawTrunk(svg,cx,sampleY,hw,pg.g,opts);
    drawMainBumps(svg,cx,hwAtY,yForMs,pg,opts);

    let wtIndex=0;
    for(const [wtName,wtEvents] of pg.byWt){
      drawWorktree(svg,cx,W,wtName,wtEvents,sampleY,hw,wtIndex,t0,t1,plotTop,plotBottom,opts);
      wtIndex++;
    }
    drawBudAndLabel(svg,defs,cx,plotTop,pg.g,pg.total,gi,opts);
  });

  drawRootCaption(svg,ML,plotBottom,t0,H,opts);
}
