/* Rooted commit graph — one column per project, one dot per real commit.
   Time runs bottom (oldest/"root", hollow ring) to top (newest/"now", glowing
   tip), matching the vertical growth reading shared by all three timeline
   renderers. Main-lane commits (worktree===null) sit on a fixed centerline;
   each distinct worktree gets its own offset lane, fanned out and joined to
   the main lane with approximate (non-topological) fork/rejoin curves — we
   have no real parent SHAs, so these are visual "started around here" hints,
   not asserted git structure. No aggregation: every commit in grouped.groups
   is rendered and individually hoverable, by design of this renderer. */
export function renderCommitGraph(svg, W, H, grouped, opts) {
  const SVGNS = 'http://www.w3.org/2000/svg';
  const FONT = "system-ui,-apple-system,'Segoe UI',sans-serif";
  const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);
  const mk = (tag) => document.createElementNS(SVGNS, tag);
  const set = (el, attrs) => {
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  };
  const txt = (content, attrs) => {
    const t = mk('text');
    t.textContent = content;
    return set(t, Object.assign({ 'font-family': FONT }, attrs));
  };

  const groups = grouped.groups || [];
  const n = groups.length;
  if (!n || W < 20 || H < 20) return;

  // ---- layout ----
  const marginT = clamp(H * 0.1, 34, 48);
  const marginB = clamp(H * 0.05, 18, 26);
  const marginL = clamp(W * 0.02, 10, 20);
  const marginR = clamp(W * 0.02, 10, 20);
  const plotTop = marginT;
  const plotBottom = H - marginB;
  const usableW = Math.max(W - marginL - marginR, 20);
  const colW = usableW / n;

  const t0 = grouped.t0, t1 = grouped.t1;
  const yFor = (ms) => {
    if (!(t1 > t0)) return (plotTop + plotBottom) / 2;
    const f = clamp((ms - t0) / (t1 - t0), 0, 1);
    return plotBottom - f * (plotBottom - plotTop);
  };

  const defs = mk('defs');
  svg.appendChild(defs);

  // subtle "now" glow across the top band
  const glowId = 'ccg-now-glow';
  const rg = set(mk('radialGradient'), { id: glowId, cx: '0.5', cy: '0', r: '0.9' });
  rg.appendChild(set(mk('stop'), { offset: '0', 'stop-color': opts.colorAccent, 'stop-opacity': '0.08' }));
  rg.appendChild(set(mk('stop'), { offset: '1', 'stop-color': opts.colorAccent, 'stop-opacity': '0' }));
  defs.appendChild(rg);
  svg.appendChild(set(mk('rect'), {
    x: 0, y: plotTop - marginT * 0.6, width: W, height: (plotBottom - plotTop) * 0.32,
    fill: `url(#${glowId})`,
  }));

  // z-order layers, shared across all groups so nothing from group A ever
  // paints over group B's dots (columns are spatially disjoint anyway, but
  // this also keeps every group's own lines strictly under its own dots).
  const washLayer = mk('g');
  const gridLayer = mk('g');
  const linesLayer = mk('g');
  const dotsLayer = mk('g');
  const labelsLayer = mk('g');
  const cornerLayer = mk('g');
  const hitLayer = mk('g'); // topmost: guarantees every commit stays hoverable
  [washLayer, gridLayer, linesLayer, dotsLayer, labelsLayer, cornerLayer, hitLayer].forEach((l) => svg.appendChild(l));

  for (let i = 0; i <= 4; i++) {
    const y = plotTop + ((plotBottom - plotTop) * i) / 4;
    gridLayer.appendChild(set(mk('line'), {
      x1: marginL, y1: y.toFixed(1), x2: W - marginR, y2: y.toFixed(1),
      stroke: opts.colorLine, 'stroke-width': 1,
    }));
  }
  cornerLayer.appendChild(txt('oldest: ' + opts.fmtDate(t0), {
    x: marginL, y: H - 6, 'font-size': 8.5, fill: opts.colorInk3,
  }));
  cornerLayer.appendChild(txt('NOW ↑', {
    x: W - marginR, y: plotTop - 8, 'text-anchor': 'end', 'font-size': 9,
    fill: opts.colorAccent, 'letter-spacing': '0.5',
  }));

  let gradSeq = 0;
  const magRadius = (e) => {
    const m = (Number(e.insertions) || 0) + (Number(e.deletions) || 0);
    return clamp(2.2 + Math.sqrt(m) * 0.26, 2.2, 8);
  };

  // Connects consecutive same-lane commits with a gentle alternating-bow
  // curve (fixed x, deterministic jitter by pair index) rather than a plain
  // straight line, to read as a climbing "vine" like the validated mockup.
  function vine(x, pts, parent) {
    const jitMag = clamp(colW * 0.06, 2, 6);
    for (let i = 1; i < pts.length; i++) {
      const y0 = pts[i - 1].y, y1 = pts[i].y;
      if (Math.abs(y0 - y1) < 0.01) continue;
      const mid = (y0 + y1) / 2;
      const jit = (i % 2 === 0 ? 1 : -1) * jitMag;
      const d = `M${x.toFixed(1)},${y0.toFixed(1)} Q${(x + jit).toFixed(1)},${mid.toFixed(1)} ${x.toFixed(1)},${y1.toFixed(1)}`;
      parent.appendChild(set(mk('path'), {
        d, fill: 'none', stroke: opts.colorInk, 'stroke-opacity': 0.42,
        'stroke-width': 1, 'stroke-linecap': 'round',
      }));
    }
  }

  // fork/rejoin transition between main and worktree lanes — approximate,
  // not a real git parent edge, hence dashed + accent-colored like the mockup.
  function transitionCurve(x0, y0, x1, y1, parent) {
    const bowY = Math.min(y0, y1) - 4;
    const d = `M${x0.toFixed(1)},${y0.toFixed(1)} Q${((x0 + x1) / 2).toFixed(1)},${bowY.toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
    parent.appendChild(set(mk('path'), {
      d, fill: 'none', stroke: opts.colorAccent, 'stroke-opacity': 0.5,
      'stroke-width': 1.1, 'stroke-dasharray': '0.2 3', 'stroke-linecap': 'round',
    }));
  }

  // no main-lane commit follows a worktree's last commit — trail it off
  // with a fading stroke instead of asserting a rejoin that isn't there.
  function taper(x, y, parent) {
    gradSeq++;
    const id = 'ccg-taper-' + gradSeq;
    const y2 = y - 16;
    const lg = set(mk('linearGradient'), { id, x1: x, y1: y, x2: x, y2, gradientUnits: 'userSpaceOnUse' });
    lg.appendChild(set(mk('stop'), { offset: 0, 'stop-color': opts.colorInk, 'stop-opacity': 0.4 }));
    lg.appendChild(set(mk('stop'), { offset: 1, 'stop-color': opts.colorInk, 'stop-opacity': 0 }));
    defs.appendChild(lg);
    const d = `M${x.toFixed(1)},${y.toFixed(1)} Q${(x + 3).toFixed(1)},${(y - 8).toFixed(1)} ${(x - 1).toFixed(1)},${y2.toFixed(1)}`;
    parent.appendChild(set(mk('path'), {
      d, fill: 'none', stroke: `url(#${id})`, 'stroke-width': 1.1,
      'stroke-dasharray': '0.2 3', 'stroke-linecap': 'round',
    }));
  }

  function addTicks(x, y, r, e, parent) {
    const ins = Number(e.insertions) || 0, del = Number(e.deletions) || 0;
    if (ins > 0) {
      const tipx = x + r + 2.6;
      parent.appendChild(set(mk('polygon'), {
        points: `${x.toFixed(1)},${(y - 2.1).toFixed(1)} ${x.toFixed(1)},${(y + 2.1).toFixed(1)} ${tipx.toFixed(1)},${y.toFixed(1)}`,
        fill: opts.colorIns, 'fill-opacity': 0.75,
      }));
    }
    if (del > 0) {
      const tipx = x - r - 2.6;
      parent.appendChild(set(mk('polygon'), {
        points: `${x.toFixed(1)},${(y - 2.1).toFixed(1)} ${x.toFixed(1)},${(y + 2.1).toFixed(1)} ${tipx.toFixed(1)},${y.toFixed(1)}`,
        fill: opts.colorDel, 'fill-opacity': 0.75,
      }));
    }
  }

  // Hit target is sized off the *visual* radius (which can exceed the base
  // dot for root/tip markers), floored at 6px per the brief's >=5px rule.
  function addHit(x, y, visualR, e) {
    const c = set(mk('circle'), {
      cx: x.toFixed(1), cy: y.toFixed(1), r: Math.max(6, visualR + 2),
      fill: '#000', 'fill-opacity': 0, 'pointer-events': 'all',
    });
    c.style.cursor = 'pointer';
    c.addEventListener('pointerenter', (ev) => opts.onHover(e, ev.clientX, ev.clientY));
    c.addEventListener('pointermove', (ev) => opts.onMove(ev.clientX, ev.clientY));
    c.addEventListener('pointerleave', () => opts.onLeave());
    hitLayer.appendChild(c);
  }

  groups.forEach((g, gi) => {
    const colX0 = marginL + colW * gi;
    const cx = colX0 + colW / 2;
    const events = g.events || [];
    const fontMain = colW < 70 ? 9.5 : 11.5;

    const padW = Math.min(6, colW * 0.08);
    washLayer.appendChild(set(mk('rect'), {
      x: (colX0 + padW).toFixed(1), y: (plotTop - 6).toFixed(1),
      width: Math.max(0, colW - 2 * padW).toFixed(1), height: (plotBottom - plotTop + 10).toFixed(1),
      rx: 10, fill: opts.hexA(g.color, 0.04),
    }));

    labelsLayer.appendChild(txt(g.label, {
      x: cx, y: plotTop - 18, 'text-anchor': 'middle', 'font-size': fontMain,
      'font-weight': 600, fill: opts.colorInk,
    }));
    labelsLayer.appendChild(txt(events.length + (events.length === 1 ? ' commit' : ' commits'), {
      x: cx, y: plotTop - 7, 'text-anchor': 'middle', 'font-size': 7.5, fill: opts.colorInk3,
    }));

    if (!events.length) return; // column with no commits in this window/selection

    const withMs = events.map((e) => ({ e, ms: +new Date(e.date) || 0, y: 0 }));
    withMs.sort((a, b) => a.ms - b.ms);
    withMs.forEach((p) => { p.y = yFor(p.ms); });

    const mainPts = [];
    const wtOrder = [];
    const wtMap = new Map();
    for (const p of withMs) {
      const wt = p.e.worktree;
      if (wt) {
        if (!wtMap.has(wt)) { wtMap.set(wt, []); wtOrder.push(wt); }
        wtMap.get(wt).push(p);
      } else {
        mainPts.push(p);
      }
    }

    const maxOffset = Math.max(8, colW / 2 - 14);
    const step = clamp(colW * 0.16, 8, 22);
    const laneX = new Map();
    wtOrder.forEach((name, idx) => {
      const side = idx % 2 === 0 ? 1 : -1;
      const mag = Math.floor(idx / 2) + 1;
      laneX.set(name, cx + side * Math.min(step * mag, maxOffset));
    });

    vine(cx, mainPts, linesLayer);
    wtOrder.forEach((name) => {
      const pts = wtMap.get(name);
      const lx = laneX.get(name);
      vine(lx, pts, linesLayer);
      const first = pts[0], last = pts[pts.length - 1];
      transitionCurve(cx, first.y, lx, first.y, linesLayer);

      let rejoin = null;
      for (const mp of mainPts) {
        if (mp.ms > last.ms && (!rejoin || mp.ms < rejoin.ms)) rejoin = mp;
      }
      if (rejoin) transitionCurve(lx, last.y, cx, rejoin.y, linesLayer);
      else taper(lx, last.y, linesLayer);

      if (wtOrder.length <= 3 && colW >= 50) {
        const anchor = lx >= cx ? 'start' : 'end';
        const lx2 = lx >= cx ? lx + 6 : lx - 6;
        labelsLayer.appendChild(txt(name, {
          x: lx2.toFixed(1), y: (first.y - 6).toFixed(1), 'text-anchor': anchor,
          'font-size': 6.6, fill: opts.colorInk3, 'fill-opacity': 0.75,
        }));
      }
    });

    const rootP = withMs[0], tipP = withMs[withMs.length - 1];
    withMs.forEach((p) => {
      const x = p.e.worktree ? laneX.get(p.e.worktree) : cx;
      const r = magRadius(p.e);
      const isRoot = p === rootP, isTip = p === tipP;
      let visualR = r;
      if (isTip) {
        visualR = r + 5;
        dotsLayer.appendChild(set(mk('circle'), {
          cx: x.toFixed(1), cy: p.y.toFixed(1), r: visualR.toFixed(1),
          fill: opts.colorAccent, 'fill-opacity': 0.16,
        }));
        dotsLayer.appendChild(set(mk('circle'), {
          cx: x.toFixed(1), cy: p.y.toFixed(1), r: Math.max(r * 0.7, 3).toFixed(1),
          fill: opts.colorAccent,
        }));
        if (isRoot) { // single-commit group: it's both root and tip
          dotsLayer.appendChild(set(mk('circle'), {
            cx: x.toFixed(1), cy: p.y.toFixed(1), r: (r + 2).toFixed(1),
            fill: 'none', stroke: opts.colorAccent, 'stroke-width': 1, 'stroke-opacity': 0.6,
          }));
        }
      } else if (isRoot) {
        visualR = r + 2;
        dotsLayer.appendChild(set(mk('circle'), {
          cx: x.toFixed(1), cy: p.y.toFixed(1), r: Math.max(r, 3.4).toFixed(1),
          fill: 'none', stroke: opts.colorAccent, 'stroke-width': 1.1, 'stroke-opacity': 0.85,
        }));
        dotsLayer.appendChild(set(mk('circle'), {
          cx: x.toFixed(1), cy: p.y.toFixed(1), r: 1.2, fill: opts.colorAccent,
        }));
      } else {
        dotsLayer.appendChild(set(mk('circle'), {
          cx: x.toFixed(1), cy: p.y.toFixed(1), r: r.toFixed(1),
          fill: opts.colorInk, 'fill-opacity': 0.8,
          stroke: opts.hexA(opts.colorInk, 0.2), 'stroke-width': 0.6,
        }));
      }
      addTicks(x, p.y, r, p.e, dotsLayer);
      addHit(x, p.y, visualR, p.e);
    });

    if (tipP.y - plotTop > 10) {
      const tipX = tipP.e.worktree ? laneX.get(tipP.e.worktree) : cx;
      labelsLayer.appendChild(set(mk('line'), {
        x1: tipX, y1: plotTop - 2, x2: tipX, y2: Math.max(plotTop + 2, tipP.y - 6),
        stroke: opts.colorLine, 'stroke-width': 0.8, 'stroke-dasharray': '0.5 2.4',
      }));
    }
  });
}
