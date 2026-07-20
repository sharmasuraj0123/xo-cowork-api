/* Sessions space renderer: one ThemeRiver-style ribbon per project, time
   running bottom (t0/oldest) to top (t1/now). Each ribbon is two lobes off a
   thin vertical spine — insertions bulge right, deletions bulge left — built
   from weekly-ish time buckets so per-minute edit bursts read as a smooth
   flow instead of noise. Worktree activity braids in as a lighter secondary
   ribbon alongside the spine, only across the buckets where it's actually
   present.

   Atomic hover unit is a BUCKET (not a raw event) per the render contract —
   raw per-minute bursts are too fine-grained to be individually meaningful
   once bucketed into a flowing shape, so each bucket gets a synthesized
   summary event (see the wireHover calls below) fired through opts.onHover
   exactly like a real one. */

const SVGNS='http://www.w3.org/2000/svg';
const mk=tag=>document.createElementNS(SVGNS,tag);
const MARGIN={t:28,r:16,b:24,l:16};

/* Candidate bucket widths, finest-first; chooseBucketMs picks the finest one
   that still keeps the bucket count at or under 60 so a 13-month "All" span
   lands on ~weekly buckets while a 1-day "Today" span lands on ~30min ones. */
const BUCKET_CANDIDATES_MS=[
  30*60e3, 60*60e3, 2*3600e3, 4*3600e3, 6*3600e3, 12*3600e3,
  24*3600e3, 2*86400e3, 3*86400e3, 7*86400e3, 14*86400e3, 30*86400e3,
];
function chooseBucketMs(spanMs){
  for(const c of BUCKET_CANDIDATES_MS)if(Math.ceil(spanMs/c)<=60)return c;
  return BUCKET_CANDIDATES_MS[BUCKET_CANDIDATES_MS.length-1];
}

/* Catmull-Rom -> cubic-bezier "C" commands continuing from the current point
   (pts[0] must already be the path's current point). Standard conversion:
   each segment's control points are pulled from the neighboring points. */
function curveThrough(pts){
  let d='';
  for(let i=0;i<pts.length-1;i++){
    const p0=pts[i-1]||pts[i],p1=pts[i],p2=pts[i+1],p3=pts[i+2]||p2;
    const c1x=p1[0]+(p2[0]-p0[0])/6,c1y=p1[1]+(p2[1]-p0[1])/6;
    const c2x=p2[0]-(p3[0]-p1[0])/6,c2y=p2[1]-(p3[1]-p1[1])/6;
    d+=`C ${c1x.toFixed(2)} ${c1y.toFixed(2)} ${c2x.toFixed(2)} ${c2y.toFixed(2)} ${p2[0].toFixed(2)} ${p2[1].toFixed(2)} `;
  }
  return d;
}

/* One ribbon lobe: a straight spine edge (baseX, yBase0->yBase1) plus a
   smooth outer edge through pts (bottom->top, i.e. ascending time order).
   pts.length===1 degenerates to a flat rectangle — no curve needed/possible
   through a single point (covers the whole-span-is-one-bucket case). */
function lobePath(baseX,yBase0,yBase1,pts){
  if(pts.length===1){
    const ox=pts[0][0];
    return `M ${baseX} ${yBase0} L ${baseX} ${yBase1} L ${ox} ${yBase1} L ${ox} ${yBase0} Z`;
  }
  const top=pts[pts.length-1];
  let d=`M ${baseX} ${yBase0} L ${baseX} ${yBase1} L ${top[0].toFixed(2)} ${top[1].toFixed(2)} `;
  d+=curveThrough(pts.slice().reverse())+'Z';
  return d;
}

function findRuns(buckets,hasFn){
  const runs=[];let start=null;
  for(let i=0;i<buckets.length;i++){
    if(hasFn(buckets[i])){if(start===null)start=i;}
    else if(start!==null){runs.push([start,i-1]);start=null;}
  }
  if(start!==null)runs.push([start,buckets.length-1]);
  return runs;
}

function truncateLabel(s,maxW,fontSize){
  const charW=fontSize*0.56;
  const maxChars=Math.max(3,Math.floor(maxW/charW));
  if(s.length<=maxChars)return s;
  return s.slice(0,Math.max(1,maxChars-1))+'…';
}

function wireHover(el,synth,opts){
  el.addEventListener('pointerenter',ev=>opts.onHover(synth,ev.clientX,ev.clientY));
  el.addEventListener('pointermove',ev=>opts.onMove(ev.clientX,ev.clientY));
  el.addEventListener('pointerleave',()=>opts.onLeave());
}

export function renderBraidedStreams(svg,W,H,grouped,opts){
  const {groups,t0,t1}=grouped;
  const x0=MARGIN.l,x1=Math.max(MARGIN.l+1,W-MARGIN.r);
  const yTop=MARGIN.t,yBottom=Math.max(MARGIN.t+1,H-MARGIN.b);
  const spanMs=Math.max(1,t1-t0);
  const bucketMs=chooseBucketMs(spanMs);
  const nBuckets=Math.max(1,Math.ceil(spanMs/bucketMs));

  const bucketStart=i=>t0+i*bucketMs;
  const bucketEnd=i=>Math.min(t1,t0+(i+1)*bucketMs);
  const bucketCenter=i=>(bucketStart(i)+bucketEnd(i))/2;
  const yForT=t=>yBottom-((t-t0)/spanMs)*(yBottom-yTop);
  /* first/last bucket forced to the plot's exact edges so every ribbon
     spans the full margin-to-margin height with no gap at either end. */
  const yForBucket=i=>i===0?yBottom:(i===nBuckets-1?yTop:yForT(bucketCenter(i)));

  const n=groups.length;
  const colW=(x1-x0)/n;

  /* Pass 1: bucket every group's events (worktree vs main lane) and find
     the global max magnitude so lobe widths are comparable across columns —
     a big project reads as visibly "fatter" than a quiet one. sqrt scaling
     (not linear) keeps one 10k-line outlier from flattening everything
     else to invisible; a floor further below guarantees any nonzero bucket
     still shows a hairline. */
  const bucketsByGroup=[];
  let globalMaxMain=0,globalMaxWT=0;
  for(const g of groups){
    const buckets=Array.from({length:nBuckets},()=>({insMain:0,delMain:0,countMain:0,insWT:0,delWT:0,countWT:0,wt:new Set()}));
    for(const e of g.events){
      const t=+new Date(e.date);
      if(!Number.isFinite(t))continue;
      let idx=Math.floor((t-t0)/bucketMs);
      if(idx<0)idx=0;else if(idx>nBuckets-1)idx=nBuckets-1;
      const b=buckets[idx];
      const ins=Math.max(0,Number(e.insertions)||0),del=Math.max(0,Number(e.deletions)||0);
      if(e.worktree){b.insWT+=ins;b.delWT+=del;b.countWT++;b.wt.add(e.worktree);}
      else{b.insMain+=ins;b.delMain+=del;b.countMain++;}
    }
    for(const b of buckets){
      if(b.insMain>globalMaxMain)globalMaxMain=b.insMain;
      if(b.delMain>globalMaxMain)globalMaxMain=b.delMain;
      if(b.insWT>globalMaxWT)globalMaxWT=b.insWT;
      if(b.delWT>globalMaxWT)globalMaxWT=b.delWT;
    }
    bucketsByGroup.push(buckets);
  }
  const gSqrtMain=Math.sqrt(globalMaxMain);
  const gSqrtWT=Math.sqrt(globalMaxWT);
  const mag2half=(v,maxHalf,gSqrt)=>{
    if(v<=0)return 0;
    if(gSqrt<=0)return 0;
    return Math.min(maxHalf,Math.max(0.6,(Math.sqrt(v)/gSqrt)*maxHalf));
  };

  /* background grounding line at t0 (bottom) — cheap, low-risk read cue */
  const base=mk('line');
  base.setAttribute('x1',x0);base.setAttribute('x2',x1);
  base.setAttribute('y1',yBottom);base.setAttribute('y2',yBottom);
  base.setAttribute('stroke',opts.colorLine);base.setAttribute('stroke-width','1');
  svg.appendChild(base);

  groups.forEach((g,gi)=>{
    const buckets=bucketsByGroup[gi];
    const cx=x0+colW*(gi+0.5);
    const spineGap=Math.min(2,colW*0.04);
    const spineLeft=cx-spineGap/2,spineRight=cx+spineGap/2;
    const colPad=Math.max(2,colW*0.07);
    const maxHalfPx=Math.max(2,Math.min(160,colW/2-colPad-spineGap/2));

    const insPts=[],delPts=[];
    for(let i=0;i<nBuckets;i++){
      const y=yForBucket(i);
      const b=buckets[i];
      insPts.push([spineRight+mag2half(b.insMain,maxHalfPx,gSqrtMain),y]);
      delPts.push([spineLeft-mag2half(b.delMain,maxHalfPx,gSqrtMain),y]);
    }

    const gGroup=mk('g');
    const pIns=mk('path');
    pIns.setAttribute('d',lobePath(spineRight,yBottom,yTop,insPts));
    pIns.setAttribute('fill',opts.colorIns);pIns.setAttribute('fill-opacity','0.82');
    pIns.setAttribute('stroke',opts.colorLine);pIns.setAttribute('stroke-width','0.6');
    gGroup.appendChild(pIns);
    const pDel=mk('path');
    pDel.setAttribute('d',lobePath(spineLeft,yBottom,yTop,delPts));
    pDel.setAttribute('fill',opts.colorDel);pDel.setAttribute('fill-opacity','0.82');
    pDel.setAttribute('stroke',opts.colorLine);pDel.setAttribute('stroke-width','0.6');
    gGroup.appendChild(pDel);
    svg.appendChild(gGroup);

    /* worktree sub-ribbon: a lighter parallel strand, only across the
       contiguous bucket runs where that project actually had worktree
       activity, so it visibly forks in and braids back out. */
    const runs=findRuns(buckets,b=>(b.insWT+b.delWT)>0);
    const subOffset=Math.min(maxHalfPx*0.28,7);
    const subBaseX=spineRight+subOffset;
    const subMaxHalf=Math.max(1.5,maxHalfPx*0.45);
    for(const [s,e] of runs){
      const pts=[];
      for(let i=s;i<=e;i++){
        pts.push([subBaseX+mag2half(buckets[i].insWT+buckets[i].delWT,subMaxHalf,gSqrtWT),yForBucket(i)]);
      }
      const yBase0=s===e?yForT(bucketStart(s)):pts[0][1];
      const yBase1=s===e?yForT(bucketEnd(s)):pts[pts.length-1][1];
      const sub=mk('path');
      sub.setAttribute('d',lobePath(subBaseX,yBase0,yBase1,pts));
      sub.setAttribute('fill',opts.hexA(g.color,0.35));
      svg.appendChild(sub);
    }

    /* main per-bucket hit-rects (wide, whole column) drawn first, then
       narrower worktree hit-rects layered on top so a hover precisely over
       the braided strand wins there and falls back to the main summary
       everywhere else in the column. */
    for(let i=0;i<nBuckets;i++){
      const b=buckets[i];
      if(b.countMain===0)continue;
      const yA=yForT(bucketStart(i)),yB=yForT(bucketEnd(i));
      const rect=mk('rect');
      rect.setAttribute('x',cx-colW/2+1);rect.setAttribute('width',Math.max(1,colW-2));
      rect.setAttribute('y',Math.min(yA,yB));rect.setAttribute('height',Math.max(0.5,Math.abs(yA-yB)));
      rect.setAttribute('fill','transparent');rect.setAttribute('pointer-events','all');
      const cnt=b.countMain;
      wireHover(rect,{
        id:`${g.id}:b${i}`,date:new Date(bucketStart(i)).toISOString(),
        project:g.id,project_label:g.label,category:null,worktree:null,
        title:`${cnt} edit${cnt===1?'':'s'}`,author:'',sha:null,
        insertions:b.insMain,deletions:b.delMain,files:[],files_count:cnt,
      },opts);
      svg.appendChild(rect);
    }
    for(const [s,e] of runs){
      for(let i=s;i<=e;i++){
        const b=buckets[i];
        if(b.countWT===0)continue; // magnitude-only run edge case: 0-diff worktree edit, nothing to size a hit target on
        const yA=yForT(bucketStart(i)),yB=yForT(bucketEnd(i));
        const rect=mk('rect');
        rect.setAttribute('x',subBaseX-2);rect.setAttribute('width',subMaxHalf+2);
        rect.setAttribute('y',Math.min(yA,yB));rect.setAttribute('height',Math.max(0.5,Math.abs(yA-yB)));
        rect.setAttribute('fill','transparent');rect.setAttribute('pointer-events','all');
        const cnt=b.countWT;
        wireHover(rect,{
          id:`${g.id}:b${i}:wt`,date:new Date(bucketStart(i)).toISOString(),
          project:g.id,project_label:g.label,category:null,worktree:[...b.wt].join(', ')||null,
          title:`${cnt} edit${cnt===1?'':'s'}`,author:'',sha:null,
          insertions:b.insWT,deletions:b.delWT,files:[],files_count:cnt,
        },opts);
        svg.appendChild(rect);
      }
    }

    const fontSize=Math.max(8,Math.min(12,colW*0.14));
    const label=mk('text');
    label.setAttribute('x',cx);label.setAttribute('y',MARGIN.t-10);
    label.setAttribute('text-anchor','middle');
    label.setAttribute('font-size',String(fontSize));
    label.setAttribute('font-weight','700');
    label.setAttribute('fill',g.color);
    label.textContent=truncateLabel(g.label,colW-6,fontSize);
    svg.appendChild(label);
  });
}
