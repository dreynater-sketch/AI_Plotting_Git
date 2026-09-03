/* FigForge editor — click, retype, resize, recolor and drag figure labels.
 *
 * The SPEC is the source of truth. Every interaction does the same thing:
 * mutate the SPEC, POST it, and swap in the SVG matplotlib renders back.
 *
 * A matplotlib re-render takes ~0.8 s, far too slow to sit in the interaction
 * loop, so it isn't in it: edits are faked in the SVG immediately and the real
 * render replaces the fake when it lands. See reconcilePreviews().
 *
 * No AI, no external calls. Everything goes to 127.0.0.1.
 */

const FIGURE = 'qcircle';

let spec = null;          // live, authoritative
let renderedSpec = null;  // what the SVG currently on screen was rendered from
let geometry = null;
let svgEl = null;
let selection = [];       // element ids; selection[0] is the primary
let history = [];
let zoom = 100;

const $ = (id) => document.getElementById(id);
const canvas = $('canvas');

/** The multi-select modifier. Ctrl on Windows/Linux, Cmd on macOS. */
const isMulti = (evt) => evt.ctrlKey || evt.metaKey;

/* ------------------------------------------------------------- plumbing */

function setStatus(msg, cls = '') {
  const el = $('status');
  el.textContent = msg;
  el.className = 'status ' + cls;
}

const clone = (o) => JSON.parse(JSON.stringify(o));

function pushHistory() {
  history.push(clone(spec));
  if (history.length > 60) history.shift();
  $('btn-undo').disabled = false;
}

let inFlight = false, needsSave = false, saveTimer = null;

function scheduleSave(delay = 0) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, delay);
}

async function doSave() {
  if (inFlight) { needsSave = true; return; }
  inFlight = true;
  setStatus('rendering…', 'busy');
  try {
    const r = await fetch(`/api/figure/${FIGURE}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ spec }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    // Keep the local spec: edits made while this render was in flight must
    // not be thrown away. Only take the server's rev.
    spec.rev = data.spec.rev;
    renderedSpec = data.spec;
    geometry = data.geometry;
    applySvg(data.svg);
    setStatus(`saved · rev ${spec.rev}`);
  } catch (e) {
    setStatus(e.message, 'err');
    // The bad edit is still sitting in `spec`. Left there, every future save
    // would resend it and fail the same way -- roll back to the last state
    // the server actually accepted, so editing can continue.
    if (renderedSpec) {
      spec = clone(renderedSpec);
      reconcilePreviews();
      refreshInspector();
    }
  } finally {
    inFlight = false;
    if (needsSave) { needsSave = false; scheduleSave(0); }
  }
}

/* ------------------------------------------------- spec element lookup */

function panelById(pid) {
  return spec.panels.find((p) => p.id === pid);
}

/** Map an element id to its place in the SPEC. Free labels and titles/axis
 *  labels keep the "t_"-prefixed svg gid as their id; a panel's own axes
 *  (for xlim/ylim/scale/aspect) use the synthetic id "panel:<panel id>". */
function resolve(id, source = spec) {
  if (!id || !source) return null;
  if (id === 'suptitle') {
    return { id, kind: 'suptitle', obj: source.suptitle, draggable: false, panel: null };
  }
  // Canonical tick-axis id (what selection actually stores) and the raw
  // per-tick svg gid (what a click on the figure hands us) both resolve to
  // the same thing: every tick label on that axis, as one group.
  const canonicalTick = id.match(/^panel:(.+):(x|y)ticks$/);
  const rawTick = !canonicalTick && id.match(/^(.+)__(x|y)tick_\d+$/);
  const tickMatch = canonicalTick || rawTick;
  if (tickMatch) {
    const [, pid, axis] = tickMatch;
    const p = source.panels.find((q) => q.id === pid);
    if (!p) return null;
    return { id: `panel:${pid}:${axis}ticks`, kind: `${axis}ticks`,
             obj: p, draggable: false, panel: pid };
  }
  if (id.startsWith('panel:')) {
    const pid = id.slice(6);
    const p = source.panels.find((q) => q.id === pid);
    if (!p) return null;
    return { id, kind: 'panel', obj: p, draggable: false, panel: pid };
  }
  const m = id.match(/^(.+)__(title|xlabel|ylabel)$/);
  if (m) {
    const p = source.panels.find((q) => q.id === m[1]);
    if (!p || !p[m[2]]) return null;
    return { id, kind: m[2], obj: p[m[2]], draggable: false, panel: p.id };
  }
  const lgMatch = id.match(/^(.+)__legend$/);
  if (lgMatch) {
    const p = source.panels.find((q) => q.id === lgMatch[1]);
    const hasLegend = p?.legend && (p.series || []).some((s) => s.label);
    if (!hasLegend) return null;
    // Position is always axes-fraction (0-1 within the panel box), not
    // data coords -- "where in the box" shouldn't jump around when the
    // data range changes the way a data-anchored point would.
    return { id, kind: 'legend', obj: p.legend, draggable: true,
             panel: p.id, coords: 'axes' };
  }
  // An endpoint handle's raw gid resolves straight to its OWN arrow (the
  // same id, same object) rather than a separate selectable thing -- the
  // arrow is what's selected either way, an endpoint click just carries a
  // hint for pointerdown about which point a drag should move. See the
  // comment there for why the hint only takes effect on an already-selected
  // arrow.
  const epMatch = id.match(/^(.+)__(p0|p1)$/);
  const arrowId = epMatch ? epMatch[1] : id;
  const pointKey = epMatch ? epMatch[2] : null;
  for (const p of source.panels) {
    for (const a of p.arrows || []) {
      if (a.id === arrowId) {
        return { id: a.id, kind: 'arrow', obj: a, draggable: true, panel: p.id, pointKey };
      }
    }
  }

  for (const p of source.panels) {
    for (const t of p.texts || []) {
      if (t.id === id) {
        return { id, kind: 'text', obj: t, draggable: true, panel: p.id,
                 coords: t.coords ?? 'data' };
      }
    }
    for (const sr of p.series || []) {
      // A curve isn't draggable -- moving data doesn't mean anything -- only
      // its style (color/width/marker) and its legend label are editable.
      if (sr.id === id) return { id, kind: 'series', obj: sr, draggable: false, panel: p.id };
    }
  }
  return null;
}

/** Every editable text in the figure, in drawing order. */
function allElements(source = spec) {
  const out = [];
  if (source.suptitle?.text) out.push(resolve('suptitle', source));
  for (const p of source.panels) {
    for (const k of ['title', 'xlabel', 'ylabel']) {
      if (p[k]?.text) out.push(resolve(`${p.id}__${k}`, source));
    }
    out.push(resolve(`${p.id}__legend`, source));
    for (const t of p.texts || []) out.push(resolve(t.id, source));
    for (const sr of p.series || []) out.push(resolve(sr.id, source));
    for (const a of p.arrows || []) out.push(resolve(a.id, source));
  }
  return out.filter(Boolean);
}

const KIND_LABEL = {
  suptitle: 'figure title', title: 'panel title', panel: 'panel axes',
  xlabel: 'x-axis label', ylabel: 'y-axis label', text: 'label',
  xticks: 'x-axis ticks', yticks: 'y-axis ticks', legend: 'legend',
  series: 'curve', arrow: 'arrow',
};

/* ------------------------------------------- coordinate transformations */

function invAxis(lim, scale, f) {
  if (scale === 'log') {
    const l0 = Math.log10(lim[0]), l1 = Math.log10(lim[1]);
    return Math.pow(10, l0 + f * (l1 - l0));
  }
  return lim[0] + f * (lim[1] - lim[0]);
}

function axisFrac(lim, scale, v) {
  if (scale === 'log') {
    const l0 = Math.log10(lim[0]), l1 = Math.log10(lim[1]);
    return (Math.log10(v) - l0) / (l1 - l0);
  }
  return (v - lim[0]) / (lim[1] - lim[0]);
}

/** SVG user units -> a panel's native coords (spec section 3.3).
 *  coords='data' inverts through the axis limits/scale (what a free
 *  label or a drawn point uses); coords='axes' is a plain 0-1 fraction of
 *  the panel box, independent of xlim/ylim/scale -- what a legend uses,
 *  since "where in the box" shouldn't jump around when the data range
 *  changes. */
function svgToData(panelId, x, y, coords = 'data') {
  const g = geometry.panels[panelId];
  const [bx, by, bw, bh] = g.bbox;
  const fx = (x - bx) / bw;
  const fy = (by + bh - y) / bh;   // SVG y runs down, data/axes y runs up
  if (coords === 'axes') return [fx, fy];
  return [invAxis(g.xlim, g.xscale, fx), invAxis(g.ylim, g.yscale, fy)];
}

/** A panel's native coords -> SVG user units, the inverse of svgToData. */
function dataToSvg(panelId, x, y, coords = 'data') {
  const g = geometry.panels[panelId];
  const [bx, by, bw, bh] = g.bbox;
  if (coords === 'axes') return [bx + x * bw, by + bh - y * bh];
  return [bx + axisFrac(g.xlim, g.xscale, x) * bw,
          by + bh - axisFrac(g.ylim, g.yscale, y) * bh];
}

/** Round to ~1/10000 of the value's natural range so spec.json stays
 *  readable. An axes-fraction's range is always exactly [0, 1]. */
function roundTo(v, lim) {
  const range = Math.abs(lim[1] - lim[0]) || 1;
  const d = Math.min(12, Math.max(0, Math.ceil(-Math.log10(range / 1e4))));
  return Number(v.toFixed(d));
}
const AXES_FRAC_LIM = [0, 1];

/** Where a drag starts from, in SVG units. Normally just the object's own
 *  stored position -- but a legend still on its `loc` preset has no stored
 *  `xy` yet, so the drag has to start from its current rendered corner
 *  (the geometry map's legend bbox) instead; the first move is what commits
 *  a real xy and switches it into free-position mode. */
function dragBaseSvg(s) {
  if (s.obj.xy) return dataToSvg(s.panel, s.obj.xy[0], s.obj.xy[1], s.coords);
  const bb = geometry.panels[s.panel]?.legend?.bbox;
  return bb ? [bb[0], bb[1]] : [0, 0];
}

function clientToSvg(evt) {
  const pt = svgEl.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  return pt.matrixTransform(svgEl.getScreenCTM().inverse());
}

/* -------------------------------------------------------- svg rendering */

function applySvg(svgText) {
  canvas.innerHTML = svgText;
  svgEl = canvas.querySelector('svg');
  svgEl.removeAttribute('width');
  svgEl.removeAttribute('height');
  applyZoom();

  for (const g of svgEl.querySelectorAll('g[id^="t_"]')) {
    const r = resolve(g.id.slice(2));
    if (!r) continue;
    if (!r.draggable) g.classList.add('ff-static');
    // A curve's own bounding box can span most of the panel, so the usual
    // bbox-padded hit rect (right for a compact text label) would swallow
    // clicks meant for anything drawn on top of it. Its click target is
    // already the wide invisible stroke the server drew alongside it. Same
    // reasoning for an arrow: a diagonal one's bbox covers a rectangle well
    // beyond its actual line, and the server already drew appropriately
    // narrow/small hit targets along its body and at each endpoint.
    if (r.kind !== 'series' && r.kind !== 'arrow') addHitTarget(g);
  }
  reconcilePreviews();
  drawOutlines();
}

function applyZoom() {
  if (!svgEl || !geometry) return;
  svgEl.style.width = (geometry.width * zoom / 100) + 'px';
  svgEl.style.height = (geometry.height * zoom / 100) + 'px';
}

function groupFor(id) {
  return svgEl ? svgEl.querySelector(`g[id="t_${CSS.escape(id)}"]`) : null;
}

/* matplotlib draws glyphs as paths, so a label without a background box is
 * only clickable on the letter strokes themselves. Lay a transparent rect
 * over each label's bounding box so the whole label is grabbable. It lives
 * inside the group, so it moves with the drag transform. */
function addHitTarget(g) {
  let bb;
  try { bb = g.getBBox(); } catch { return; }
  if (!bb.width || !bb.height) return;
  const pad = 2;
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('class', 'ff-hit');
  rect.setAttribute('x', bb.x - pad);
  rect.setAttribute('y', bb.y - pad);
  rect.setAttribute('width', bb.width + 2 * pad);
  rect.setAttribute('height', bb.height + 2 * pad);
  rect.setAttribute('fill', 'transparent');
  rect.setAttribute('pointer-events', 'all');
  g.insertBefore(rect, g.firstChild);
}

function clearOutlines() {
  svgEl?.querySelectorAll('.ff-outline, .ff-arrow-handle').forEach((n) => n.remove());
}

/** A draggable endpoint handle for a selected arrow. cx/cy are updated
 *  directly (no getBBox involved) so redrawing them on every drag frame is
 *  cheap -- unlike the outline rects, which the mid-drag path deliberately
 *  avoids recomputing every frame. */
function mkArrowHandle(arrowId, pointKey, x, y) {
  const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  c.setAttribute('class', 'ff-arrow-handle');
  c.dataset.arrow = arrowId;
  c.dataset.point = pointKey;
  c.setAttribute('cx', x);
  c.setAttribute('cy', y);
  c.setAttribute('r', 5);
  c.setAttribute('fill', '#1f6feb');
  c.setAttribute('stroke', 'white');
  c.setAttribute('stroke-width', '1.5');
  c.setAttribute('pointer-events', 'none');
  return c;
}

/** Reposition existing handle circles from the live spec, without touching
 *  the DOM structure -- called on every drag frame, so it must stay cheap. */
function updateArrowHandlePositions() {
  svgEl?.querySelectorAll('.ff-arrow-handle').forEach((h) => {
    const r = resolve(h.dataset.arrow);
    if (!r) return;
    const pt = r.obj[h.dataset.point];
    if (!pt) return;
    const [x, y] = dataToSvg(r.panel, pt[0], pt[1]);
    h.setAttribute('cx', x);
    h.setAttribute('cy', y);
  });
}

function mkOutlineRect(x, y, w, h, primary) {
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('class', 'ff-outline');
  rect.setAttribute('x', x);
  rect.setAttribute('y', y);
  rect.setAttribute('width', w);
  rect.setAttribute('height', h);
  rect.setAttribute('fill', 'none');
  rect.setAttribute('stroke', '#1f6feb');
  rect.setAttribute('stroke-width', primary ? '1.2' : '1');
  rect.setAttribute('stroke-dasharray', primary ? '' : '4 3');
  rect.setAttribute('pointer-events', 'none');
  return rect;
}

/** Selection boxes. A label's outline is inserted as a sibling so it shares
 *  the label's own drag/resize transform; a panel's outline is drawn straight
 *  from the geometry map, since axes never move or preview-transform. */
function drawOutlines() {
  clearOutlines();
  if (!svgEl) return;
  selection.forEach((id, i) => {
    const tickMatch = id.match(/^panel:(.+):(x|y)ticks$/);
    if (tickMatch) {
      const [, pid, axis] = tickMatch;
      const els = svgEl.querySelectorAll(
        `g[id^="t_${CSS.escape(pid)}__${axis}tick_"]`);
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      els.forEach((g) => {
        let bb;
        try { bb = g.getBBox(); } catch { return; }
        if (!bb.width || !bb.height) return;
        x0 = Math.min(x0, bb.x); y0 = Math.min(y0, bb.y);
        x1 = Math.max(x1, bb.x + bb.width); y1 = Math.max(y1, bb.y + bb.height);
      });
      if (!isFinite(x0)) return;
      const pad = 3;
      svgEl.appendChild(mkOutlineRect(
        x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad, i === 0));
      return;
    }
    if (id.startsWith('panel:')) {
      const bb = geometry.panels[id.slice(6)]?.bbox;
      if (!bb) return;
      svgEl.appendChild(mkOutlineRect(bb[0], bb[1], bb[2], bb[3], i === 0));
      return;
    }
    const g = groupFor(id);
    if (!g) return;
    let bb;
    try { bb = g.getBBox(); } catch { return; }
    const pad = 3;
    const rect = mkOutlineRect(bb.x - pad, bb.y - pad, bb.width + 2 * pad, bb.height + 2 * pad, i === 0);
    const tf = g.getAttribute('transform');
    if (tf) rect.setAttribute('transform', tf);
    g.parentNode.insertBefore(rect, g.nextSibling);

    const r = resolve(id);
    if (r?.kind === 'arrow') {
      const p0 = dataToSvg(r.panel, r.obj.p0[0], r.obj.p0[1]);
      const p1 = dataToSvg(r.panel, r.obj.p1[0], r.obj.p1[1]);
      svgEl.appendChild(mkArrowHandle(id, 'p0', p0[0], p0[1]));
      svgEl.appendChild(mkArrowHandle(id, 'p1', p1[0], p1[1]));
    }
  });
}

/* ------------------------------------------------- optimistic previews
 *
 * These previews are exact, not approximations. matplotlib emits each line of
 * text as <g style="fill: COLOR" transform="translate(ax ay) scale(s -s)">
 * with s = fontsize/100, and glyph advances, line spacing and the box's
 * padding are all linear in the font size. So scaling the whole group about
 * the text's anchor is precisely what matplotlib itself would draw.
 *
 * Everything is derived by diffing the live SPEC against `renderedSpec`, so
 * it is stateless and self-correcting: if a render lands while further edits
 * are queued, the leftover difference is simply re-applied on top.
 */

/** The anchor a text grows from. Free labels have an exact one in the
 *  geometry map; for titles and axis labels matplotlib decides the position
 *  at draw time, so read it back out of the SVG the renderer produced. */
function anchorFor(id, g) {
  const known = geometry.texts?.[id]?.anchor;
  if (known) return known;
  for (const c of g.children) {
    if (c.id && c.id.startsWith('patch')) continue;
    const tf = c.getAttribute?.('transform');
    const m = tf && tf.match(/translate\(\s*([-\d.eE]+)[\s,]+([-\d.eE]+)\s*\)/);
    if (m) return [parseFloat(m[1]), parseFloat(m[2])];
  }
  return null;
}

function reconcilePreviews() {
  if (!renderedSpec || !svgEl) return;

  for (const el of allElements(spec)) {
    if (el.kind === 'legend') continue;  // handled separately below
    const old = resolve(el.id, renderedSpec);
    const g = groupFor(el.id);
    if (!old || !g) continue;

    const anchor = anchorFor(el.id, g);
    if (!anchor) continue;

    // Position: how far the anchor has moved since this SVG was rendered.
    let dx = 0, dy = 0;
    if (el.draggable && old.obj.xy &&
        (old.obj.xy[0] !== el.obj.xy[0] || old.obj.xy[1] !== el.obj.xy[1])) {
      const a = dataToSvg(el.panel, old.obj.xy[0], old.obj.xy[1]);
      const b = dataToSvg(el.panel, el.obj.xy[0], el.obj.xy[1]);
      dx = b[0] - a[0];
      dy = b[1] - a[1];
    }
    // Size: scale about the anchor, which is where matplotlib grows from.
    const k = (el.obj.size ?? 12) / (old.obj.size ?? 12);

    setPreviewTransform(g, anchor, dx, dy, k);

    if ((el.obj.color ?? '#000000') !== (old.obj.color ?? '#000000')) {
      setGlyphFill(g, el.obj.color ?? '#000000');
    }
  }

  // Tick labels have no stored SPEC id per glyph -- matplotlib regenerates
  // the actual tick set on every limit change -- but the same
  // scale-about-anchor trick still applies: find every currently-rendered
  // tick-label group for a panel whose xtick_size/ytick_size just changed,
  // and scale each one about its own anchor. Without this, Ctrl+Shift+>/<
  // on a tick-axis selection had no feedback at all until the real render
  // landed -- a ~1s round trip, since every new tick size also busts the
  // tight_layout cache -- making the same shortcut that feels instant on a
  // free label feel badly broken here.
  for (const p of spec.panels) {
    const oldPanel = renderedSpec.panels.find((q) => q.id === p.id);
    if (!oldPanel) continue;
    for (const axis of ['x', 'y']) {
      const key = `${axis}tick_size`;
      const oldSize = oldPanel[key] ?? 11;
      const newSize = p[key] ?? 11;
      if (oldSize === newSize) continue;
      const k = newSize / oldSize;
      svgEl.querySelectorAll(`g[id^="t_${CSS.escape(p.id)}__${axis}tick_"]`)
        .forEach((g) => {
          const anchor = anchorFor(g.id.slice(2), g);
          if (anchor) setPreviewTransform(g, anchor, 0, 0, k);
        });
    }
  }

  // A legend is a compound group (frame + sample lines + text) with no
  // single inner translate the way a text label has, so anchorFor() can't
  // find it reliably -- use the geometry-provided bbox corner instead. That
  // bbox always reflects exactly what renderedSpec currently shows (the
  // server computed it from that spec), so it doubles as both the anchor to
  // scale from AND the "old" position to measure a drag delta against --
  // including the one-time preset-loc -> dragged-xy transition, since the
  // bbox corner is correct either way.
  for (const p of spec.panels) {
    if (!p.legend) continue;
    const oldPanel = renderedSpec.panels.find((q) => q.id === p.id);
    if (!oldPanel) continue;
    const g = groupFor(`${p.id}__legend`);
    const bb = geometry.panels[p.id]?.legend?.bbox;
    if (!g || !bb) continue;
    const anchor = [bb[0], bb[1]];

    let dx = 0, dy = 0;
    if (p.legend.xy) {
      const newSvg = dataToSvg(p.id, p.legend.xy[0], p.legend.xy[1], 'axes');
      dx = newSvg[0] - bb[0];
      dy = newSvg[1] - bb[1];
    }
    const k = (p.legend.size ?? 10) / (oldPanel.legend?.size ?? 10);
    setPreviewTransform(g, anchor, dx, dy, k);
  }

  // An arrow's visible shape is one FancyArrowPatch artist (shaft + heads
  // together), separately gid-tagged from its invisible hit-line. Moving
  // both endpoints by the identical delta -- a whole-arrow drag -- is an
  // honest rigid translate, so it gets a real instant preview on both
  // pieces. Moving just ONE endpoint changes the angle and length, which
  // would need the arrowhead geometry recomputed, not just transformed --
  // faking that would visibly distort the head, so it's intentionally left
  // to settle on the real render instead (the grabbed handle itself still
  // tracks the cursor immediately either way; see updateArrowHandlePositions).
  for (const p of spec.panels) {
    const oldPanel = renderedSpec.panels.find((q) => q.id === p.id);
    if (!oldPanel) continue;
    for (const a of p.arrows || []) {
      const oldA = (oldPanel.arrows || []).find((x) => x.id === a.id);
      if (!oldA) continue;
      const dx0 = a.p0[0] - oldA.p0[0], dy0 = a.p0[1] - oldA.p0[1];
      const dx1 = a.p1[0] - oldA.p1[0], dy1 = a.p1[1] - oldA.p1[1];
      if (!dx0 && !dy0 && !dx1 && !dy1) continue;
      if (Math.abs(dx0 - dx1) > 1e-9 || Math.abs(dy0 - dy1) > 1e-9) continue;
      const a0 = dataToSvg(p.id, oldA.p0[0], oldA.p0[1]);
      const a1 = dataToSvg(p.id, a.p0[0], a.p0[1]);
      const tf = `translate(${a1[0] - a0[0]} ${a1[1] - a0[1]})`;
      const vis = groupFor(a.id + '__vis');
      const hit = groupFor(a.id);
      if (vis) vis.setAttribute('transform', tf);
      if (hit) hit.setAttribute('transform', tf);
    }
  }

  if (!selection.length) return;
  if (drag) {
    // Mid-drag, recomputing getBBox every frame forces a synchronous layout.
    // The outline shapes haven't changed, so just carry the same transforms.
    // Arrow handles are cheap to reposition directly (no getBBox), so they
    // still track the cursor every frame even when the shape itself doesn't.
    updateArrowHandlePositions();
    for (const id of selection) {
      const g = groupFor(id);
      const o = g?.nextElementSibling;
      if (!o || !o.classList.contains('ff-outline')) continue;
      const tf = g.getAttribute('transform');
      if (tf) o.setAttribute('transform', tf); else o.removeAttribute('transform');
    }
  } else {
    drawOutlines();
  }
}

function setPreviewTransform(g, anchor, dx, dy, k) {
  if (!dx && !dy && Math.abs(k - 1) < 1e-9) {
    g.removeAttribute('transform');
    return;
  }
  const parts = [];
  if (dx || dy) parts.push(`translate(${dx} ${dy})`);
  if (Math.abs(k - 1) > 1e-9) {
    parts.push(`translate(${anchor[0]} ${anchor[1]})`,
               `scale(${k})`,
               `translate(${-anchor[0]} ${-anchor[1]})`);
  }
  g.setAttribute('transform', parts.join(' '));
}

/** Recolor the glyphs but never the background box, which is its own patch. */
function setGlyphFill(g, color) {
  for (const child of g.children) {
    if (child.id && child.id.startsWith('patch')) continue;
    if (child.classList.contains('ff-hit')) continue;
    if (child.tagName.toLowerCase() === 'g') child.style.fill = color;
  }
}

/* ------------------------------------------------------------ selection */

function setSelection(ids) {
  selection = [...new Set(ids)].filter((id) => resolve(id));
  refreshInspector();
  refreshPeerBar();
  markList();
  drawOutlines();
}

function toggleSelection(id) {
  const i = selection.indexOf(id);
  if (i === -1) setSelection([...selection, id]);
  else setSelection(selection.filter((s) => s !== id));
}

const selected = () => selection.map((id) => resolve(id)).filter(Boolean);

/** One value if every selected element agrees, otherwise undefined. */
function common(get) {
  const vals = selected().map(get);
  if (!vals.length) return undefined;
  return vals.every((v) => v === vals[0]) ? vals[0] : undefined;
}

function refreshInspector() {
  const sel = selected();
  $('insp-empty').hidden = sel.length > 0;
  $('insp-body').hidden = sel.length === 0;
  refreshDataPanel(sel);
  if (!sel.length) return;

  const multi = sel.length > 1;
  const kinds = [...new Set(sel.map((s) => s.kind))];
  $('insp-kind').textContent = kinds.length === 1
    ? KIND_LABEL[kinds[0]] : 'mixed';
  $('insp-id').textContent = multi ? `${sel.length} selected` : sel[0].id;

  const allPanels = sel.every((s) => s.kind === 'panel'
    || s.kind === 'xticks' || s.kind === 'yticks');
  const allLegends = sel.every((s) => s.kind === 'legend');
  const allSeries = sel.every((s) => s.kind === 'series');
  const allArrows = sel.every((s) => s.kind === 'arrow');
  $('label-fields').hidden = allPanels || allLegends || allSeries || allArrows;
  $('axes-fields').hidden = !allPanels;
  $('legend-fields').hidden = !allLegends;
  $('series-fields').hidden = !allSeries;
  $('arrow-fields').hidden = !allArrows;
  if (allPanels) { refreshAxesInspector(); return; }
  if (allLegends) { refreshLegendInspector(); return; }
  if (allSeries) { refreshSeriesInspector(); return; }
  if (allArrows) { refreshArrowInspector(); return; }

  // Retyping many labels at once is meaningless; offer it only for one.
  $('text-field').hidden = multi;
  $('symbol-palette').hidden = multi;
  $('text-note').hidden = multi;
  if (!multi) $('f-text').value = sel[0].obj.text ?? '';

  const size = common((s) => s.obj.size ?? 12);
  $('f-size').value = size ?? '';
  $('f-size').placeholder = size === undefined ? 'mixed' : '';

  const color = common((s) => normHex(s.obj.color ?? '#000000'));
  $('f-color').value = color ?? '#000000';
  $('f-color-hex').value = color ?? '';
  $('f-color-hex').placeholder = color === undefined ? 'mixed' : '';

  // Coordinates only make sense for a single free label.
  const single = !multi && sel[0].draggable;
  $('xy-group').hidden = !single;
  if (single) {
    const p = panelById(sel[0].panel);
    $('f-x').value = sel[0].obj.xy[0];
    $('f-y').value = sel[0].obj.xy[1];
    $('f-xunit').textContent = `(${stripMath(p.xlabel?.text) || 'data'})`;
    $('f-yunit').textContent = `(${stripMath(p.ylabel?.text) || 'data'})`;
  }

  // Alignment and the background box apply to any set of free labels.
  const allText = sel.every((s) => s.kind === 'text');
  $('align-group').hidden = !allText;
  if (allText) {
    $('f-ha').value = common((s) => s.obj.ha ?? 'left') ?? 'left';
    $('f-va').value = common((s) => s.obj.va ?? 'baseline') ?? 'baseline';
    const boxed = common((s) => !!s.obj.bbox);
    $('f-box').indeterminate = boxed === undefined;
    $('f-box').checked = boxed === true;
  }
}

/** Axis limits/scale/aspect for one or more selected panels. Bulk edits set
 *  each panel's own array index (e.g. xlim[0]) rather than sharing one array,
 *  so panels keep their own range on the axis you didn't touch. */
function refreshAxesInspector() {
  const setNum = (id, get) => {
    const v = common(get);
    $(id).value = v ?? '';
    $(id).placeholder = v === undefined ? 'mixed' : '';
  };

  // A tick click selects one axis, not the whole panel -- so only show
  // the fields for the axis (or axes) actually present in the selection.
  // A plain panel selection (blank plot area) counts as both, matching
  // what it always showed; mixing an x-tick and a y-tick selection (even
  // from different panels) shows both, same as a panel selection would.
  const kinds = new Set(selected().map((s) => s.kind));
  const showX = kinds.has('panel') || kinds.has('xticks');
  const showY = kinds.has('panel') || kinds.has('yticks');
  const showPanel = kinds.has('panel');
  $('axes-x-fields').hidden = !showX;
  $('axes-y-fields').hidden = !showY;
  $('axes-panel-fields').hidden = !showPanel;

  setNum('f-xmin', (s) => s.obj.xlim[0]);
  setNum('f-xmax', (s) => s.obj.xlim[1]);
  setNum('f-ymin', (s) => s.obj.ylim[0]);
  setNum('f-ymax', (s) => s.obj.ylim[1]);
  $('f-xscale').value = common((s) => s.obj.xscale ?? 'linear') ?? 'linear';
  $('f-yscale').value = common((s) => s.obj.yscale ?? 'linear') ?? 'linear';
  setNum('f-xticksize', (s) => s.obj.xtick_size ?? 11);
  setNum('f-yticksize', (s) => s.obj.ytick_size ?? 11);
  $('f-aspect').value = common((s) => s.obj.aspect ?? 'auto') ?? 'auto';
  setNum('f-framewidth', (s) => s.obj.frame_lw ?? 0.8);
  $('axes-note').hidden = true;
}

function refreshLegendInspector() {
  const sel = selected();
  const size = common((s) => s.obj.size ?? 10);
  $('f-legend-size').value = size ?? '';
  $('f-legend-size').placeholder = size === undefined ? 'mixed' : '';

  const framed = common((s) => !!s.obj.frameon);
  $('f-legend-frame').indeterminate = framed === undefined;
  $('f-legend-frame').checked = framed === true;

  $('btn-legend-reset').disabled = !sel.some((s) => s.obj.xy);
}

/** A curve's style lives under obj.style, not on obj itself (unlike every
 *  other kind so far), since that's how the SPEC already models it -- these
 *  fields write to s.obj.style.xxx rather than s.obj.xxx accordingly. */
function refreshSeriesInspector() {
  const label = common((s) => s.obj.label ?? '');
  $('f-series-label').value = label ?? '';
  $('f-series-label').placeholder = label === undefined ? 'mixed' : '(none)';

  const color = common((s) => normHex(s.obj.style?.color ?? '#000000'));
  $('f-series-color').value = color ?? '#000000';
  $('f-series-color-hex').value = color ?? '';
  $('f-series-color-hex').placeholder = color === undefined ? 'mixed' : '';

  $('f-series-marker').value = common((s) => s.obj.style?.marker ?? 'none') ?? 'none';

  const lw = common((s) => s.obj.style?.lw ?? 1.5);
  $('f-series-lw').value = lw ?? '';
  $('f-series-lw').placeholder = lw === undefined ? 'mixed' : '';

  const ms = common((s) => s.obj.style?.ms ?? 6);
  $('f-series-ms').value = ms ?? '';
  $('f-series-ms').placeholder = ms === undefined ? 'mixed' : '';

  // matplotlib's own default is fully opaque (alpha unset means 1).
  const alpha = common((s) => s.obj.style?.alpha ?? 1);
  $('f-series-alpha').value = alpha ?? 1;
  $('f-series-alpha-num').value = alpha ?? '';
  $('f-series-alpha-num').placeholder = alpha === undefined ? 'mixed' : '';
}

/** Unlike a curve, an arrow's own color/lw/arrowstyle/mutation_scale live
 *  directly on the object -- that's how the SPEC already stores them, no
 *  nested style dict to reach into. */
function refreshArrowInspector() {
  const color = common((s) => normHex(s.obj.color ?? '#000000'));
  $('f-arrow-color').value = color ?? '#000000';
  $('f-arrow-color-hex').value = color ?? '';
  $('f-arrow-color-hex').placeholder = color === undefined ? 'mixed' : '';

  $('f-arrow-style').value = common((s) => s.obj.arrowstyle ?? '->') ?? '->';

  const lw = common((s) => s.obj.lw ?? 1.8);
  $('f-arrow-lw').value = lw ?? '';
  $('f-arrow-lw').placeholder = lw === undefined ? 'mixed' : '';

  const scale = common((s) => s.obj.mutation_scale ?? 12);
  $('f-arrow-scale').value = scale ?? '';
  $('f-arrow-scale').placeholder = scale === undefined ? 'mixed' : '';
}

/** The Excel-like table below the figure: only meaningful for exactly one
 *  selected curve (showing two different curves' data at once in one table
 *  doesn't mean anything), fetched fresh from /api/data on every selection
 *  change rather than cached -- the arrays are modest (a few thousand points
 *  at most here) and this only runs on a real click, not on every render. */
let dataPanelToken = 0;

function refreshDataPanel(sel) {
  const panelEl = $('data-panel');
  const single = sel.length === 1 && sel[0].kind === 'series';
  panelEl.hidden = !single;
  if (!single) return;

  const s = sel[0];
  const p = panelById(s.panel);
  $('data-title').textContent = s.obj.label ? `${s.obj.label} (${s.id})` : s.id;
  $('data-col-x').textContent = stripMath(p.xlabel?.text) || 'x';
  $('data-col-y').textContent = stripMath(p.ylabel?.text) || 'y';
  $('data-count').textContent = '';
  $('data-status').textContent = 'loading…';
  $('data-status').className = 'data-status';
  $('data-table-body').innerHTML = '';

  const token = ++dataPanelToken;
  fetch(`/api/data/${FIGURE}/${s.id}`)
    .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
    .then(({ ok, data }) => {
      if (token !== dataPanelToken) return;  // a newer selection fired since
      if (!ok) throw new Error(data.error || 'failed to load');
      $('data-status').textContent = '';
      $('data-count').textContent = `${data.x.length} points`;
      const rows = data.x.map((x, i) =>
        `<tr><td>${fmtNum(x)}</td><td>${fmtNum(data.y[i])}</td></tr>`);
      $('data-table-body').innerHTML = rows.join('');
    })
    .catch((e) => {
      if (token !== dataPanelToken) return;
      $('data-status').textContent = e.message;
      $('data-status').className = 'data-status err';
    });
}

function fmtNum(v) {
  if (!Number.isFinite(v)) return String(v);
  if (v !== 0 && (Math.abs(v) >= 1e5 || Math.abs(v) < 1e-4)) return v.toExponential(4);
  return v.toFixed(6);
}

function normHex(c) {
  if (typeof c !== 'string') return '#000000';
  if (/^#[0-9a-f]{6}$/i.test(c)) return c.toLowerCase();
  if (c === 'black') return '#000000';
  if (c === 'white') return '#ffffff';
  const g = parseFloat(c);   // matplotlib grey strings like "0.15"
  if (!Number.isNaN(g) && g >= 0 && g <= 1 && /^[\d.]+$/.test(c)) {
    const v = Math.round(g * 255).toString(16).padStart(2, '0');
    return `#${v}${v}${v}`;
  }
  return '#000000';
}

const stripMath = (s) => (s || '').replace(/\$/g, '').replace(/\\[a-zA-Z]+/g, '').trim();

/** Apply an edit to every selected element, preview it, then persist. */
function edit(fn, { immediate = true } = {}) {
  const sel = selected();
  if (!sel.length) return;
  pushHistory();
  for (const s of sel) fn(s.obj, s);
  reconcilePreviews();
  scheduleSave(immediate ? 0 : 350);
}

/* ------------------------------------------------------- peer groups
 *
 * "You clicked a y-axis label — here is every other y-axis label." Grabbing a
 * whole family at once is what makes consistent sizing across a figure
 * tractable (spec section 6.3, house styles).
 */

function peerGroups() {
  const sel = selected();
  if (!sel.length) return [];
  const primary = sel[0];
  const all = allElements(spec);
  const groups = [];
  const add = (label, members) => {
    if (members.length > 1) groups.push({ label, ids: members.map((m) => m.id) });
  };

  if (primary.kind === 'panel') {
    add('All panels', spec.panels.map((p) => resolve(`panel:${p.id}`)).filter(Boolean));
    return groups;
  }

  if (primary.kind === 'xticks' || primary.kind === 'yticks') {
    const axis = primary.kind === 'xticks' ? 'x' : 'y';
    add(`All ${axis}-axis ticks`, spec.panels
      .map((p) => resolve(`panel:${p.id}:${axis}ticks`)).filter(Boolean));
    return groups;
  }

  if (primary.kind === 'legend') {
    // A dedicated early return, like panel/xticks/yticks: a legend has no
    // .color, so the generic "Everything in #hex" bucket below would
    // otherwise compare against a field it doesn't actually have.
    add('All legends', spec.panels.map((p) => resolve(`${p.id}__legend`)).filter(Boolean));
    return groups;
  }

  if (primary.kind === 'series') {
    // Dedicated early return, same reason as legend: a curve's size/color
    // live at different paths (no .size at all, .style.color not .color),
    // so the generic buckets below would compare the wrong thing.
    add(`All curves in panel (${primary.panel})`,
        all.filter((e) => e.kind === 'series' && e.panel === primary.panel));
    add('All curves', all.filter((e) => e.kind === 'series'));
    return groups;
  }

  if (primary.kind === 'arrow') {
    // Dedicated too: an arrow has no .size (it has mutation_scale/lw
    // instead), so the generic "Everything at Npt" bucket below would
    // compare against a field that doesn't exist.
    add(`All arrows in panel (${primary.panel})`,
        all.filter((e) => e.kind === 'arrow' && e.panel === primary.panel));
    add('All arrows', all.filter((e) => e.kind === 'arrow'));
    return groups;
  }

  if (primary.kind === 'text') {
    add(`All labels in panel (${primary.panel})`,
        all.filter((e) => e.kind === 'text' && e.panel === primary.panel));
    add('All labels', all.filter((e) => e.kind === 'text'));
  } else if (primary.kind !== 'suptitle') {
    add(`All ${KIND_LABEL[primary.kind]}s`,
        all.filter((e) => e.kind === primary.kind));
  }

  const size = primary.obj.size ?? 12;
  add(`Everything at ${size}pt`, all.filter((e) => (e.obj.size ?? 12) === size));

  const color = normHex(primary.obj.color ?? '#000000');
  const sameColor = all.filter((e) => normHex(e.obj.color ?? '#000000') === color);
  if (sameColor.length < all.length) add(`Everything in ${color}`, sameColor);

  return groups;
}

const sameSet = (a, b) =>
  a.length === b.length && [...a].sort().join() === [...b].sort().join();

function refreshPeerBar() {
  const bar = $('peerbar');
  const sel = selected();
  bar.hidden = sel.length === 0;
  if (!sel.length) return;

  const groups = $('peer-groups');
  groups.innerHTML = '';
  for (const g of peerGroups()) {
    const b = document.createElement('button');
    b.className = 'chip' + (sameSet(g.ids, selection) ? ' on' : '');
    b.innerHTML = `${escapeHtml(g.label)}<span class="n">${g.ids.length}</span>`;
    b.title = 'Click to select this group · Ctrl-click to add it';
    b.onclick = (evt) => setSelection(
      isMulti(evt) ? [...selection, ...g.ids] : g.ids);
    groups.appendChild(b);
  }
  if (!groups.children.length) {
    groups.innerHTML = '<span class="muted">no similar elements</span>';
  }

  const row = $('peer-selected-row');
  row.hidden = sel.length < 2;
  const chips = $('peer-selected');
  chips.innerHTML = '';
  if (sel.length >= 2) {
    for (const s of sel) {
      const b = document.createElement('button');
      b.className = 'chip small';
      b.innerHTML = `${escapeHtml(chipLabel(s))}<span class="x">×</span>`;
      b.title = `${s.id} — click to remove from the selection`;
      b.onclick = () => toggleSelection(s.id);
      chips.appendChild(b);
    }
  }
}

$('btn-clearsel').onclick = () => setSelection([]);

/* --------------------------------------------------------- element list */

function buildList() {
  const list = $('element-list');
  list.innerHTML = '';
  const add = (label, id, tag) => {
    const b = document.createElement('button');
    b.dataset.eid = id;
    b.innerHTML = `<span class="tag">${tag}</span>${escapeHtml(label)}`;
    b.title = `${label}\n${id}  (Ctrl-click to add to the selection)`;
    b.onclick = (evt) => {
      if (isMulti(evt)) toggleSelection(id); else setSelection([id]);
    };
    list.appendChild(b);
  };
  const group = (name) => {
    const d = document.createElement('div');
    d.className = 'group';
    d.textContent = name;
    list.appendChild(d);
  };

  group('panels');
  for (const p of spec.panels) {
    add(`panel (${p.id}) axes`, `panel:${p.id}`, '▭');
    add(`panel (${p.id}) x ticks`, `panel:${p.id}:xticks`, 'xt');
    add(`panel (${p.id}) y ticks`, `panel:${p.id}:yticks`, 'yt');
    if (resolve(`${p.id}__legend`)) add(`panel (${p.id}) legend`, `${p.id}__legend`, 'lg');
  }

  group('figure');
  if (spec.suptitle?.text) add(preview(spec.suptitle.text), 'suptitle', 'sup');

  for (const p of spec.panels) {
    group(`panel (${p.id})`);
    if (p.title?.text) add(preview(p.title.text), `${p.id}__title`, 'ttl');
    if (p.xlabel?.text) add(preview(p.xlabel.text), `${p.id}__xlabel`, 'x');
    if (p.ylabel?.text) add(preview(p.ylabel.text), `${p.id}__ylabel`, 'y');
    for (const t of p.texts || []) add(preview(t.text), t.id, '¶');
    for (const sr of p.series || []) add(sr.label || sr.id, sr.id, 'cv');
    for (const ar of p.arrows || []) add(ar.id, ar.id, '↗');
  }
  markList();
}

function preview(s) {
  return (s || '').replace(/\n/g, ' ⏎ ').slice(0, 44);
}

function chipLabel(s) {
  if (s.kind === 'panel') return `panel (${s.panel}) axes`;
  if (s.kind === 'xticks') return `panel (${s.panel}) x ticks`;
  if (s.kind === 'yticks') return `panel (${s.panel}) y ticks`;
  if (s.kind === 'legend') return `panel (${s.panel}) legend`;
  if (s.kind === 'series') return s.obj.label || s.id;
  if (s.kind === 'arrow') return s.id;
  return preview(s.obj.text);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function markList() {
  for (const b of $('element-list').querySelectorAll('button')) {
    b.classList.toggle('on', selection.includes(b.dataset.eid));
  }
}

/* --------------------------------------------------------------- drag,
 * click-to-select-a-panel, and rubber-band box select
 *
 * Empty canvas space is overloaded: on release, a click that never moved
 * selects the panel it landed in (for axis editing) or clears the selection
 * if it landed outside every panel; a click that DID move becomes a box
 * select instead. Which one it turns out to be is only known at pointerup.
 */

let drag = null;
let rubber = null;

/** Which panel (if any) an SVG-space point falls inside. */
function panelAt(pt) {
  for (const [pid, p] of Object.entries(geometry.panels)) {
    const [bx, by, bw, bh] = p.bbox;
    if (pt.x >= bx && pt.x <= bx + bw && pt.y >= by && pt.y <= by + bh) return pid;
  }
  return null;
}

const rectsIntersect = (a, b) =>
  a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

/** Grow/redraw the rubber-band div and live-update the selection under it.
 *  Tracked in screen pixels throughout, so it needs no svg coordinate math
 *  and works the same at any zoom level. */
function updateRubberBand(evt) {
  const x0 = Math.min(rubber.startClient.x, evt.clientX);
  const y0 = Math.min(rubber.startClient.y, evt.clientY);
  const x1 = Math.max(rubber.startClient.x, evt.clientX);
  const y1 = Math.max(rubber.startClient.y, evt.clientY);
  if (!rubber.el) {
    rubber.el = document.createElement('div');
    rubber.el.className = 'ff-rubber';
    document.body.appendChild(rubber.el);
  }
  Object.assign(rubber.el.style, {
    left: `${x0}px`, top: `${y0}px`, width: `${x1 - x0}px`, height: `${y1 - y0}px`,
  });

  const box = { left: x0, top: y0, right: x1, bottom: y1 };
  const hits = allElements(spec)
    .filter((el) => {
      const g = groupFor(el.id);
      return g && rectsIntersect(box, g.getBoundingClientRect());
    })
    .map((el) => el.id);
  setSelection(rubber.ctrl ? [...rubber.baseSelection, ...hits] : hits);
}

/** Double-click a label to start retyping it immediately, the way
 *  PowerPoint/Illustrator double-click into a text box. There is no real
 *  editable text in the SVG itself -- labels are matplotlib glyph paths,
 *  not text nodes -- so this jumps to the sidebar's text field (which
 *  already shows the raw mathtext source) with the content pre-selected,
 *  ready to type over. Panels and tick-axis selections have no `.text` to
 *  retype, so a double-click there is a no-op. */
canvas.addEventListener('dblclick', (evt) => {
  const g = evt.target.closest('g[id^="t_"]');
  if (!g) return;
  const r = resolve(g.id.slice(2));
  if (!r || r.obj.text === undefined) return;
  evt.preventDefault();
  setSelection([r.id]);
  const ta = $('f-text');
  ta.focus();
  ta.select();
});

/** Which gid group a click resolves to. Ordinarily the topmost one at that
 *  point, but when curves overlap almost exactly -- a fit line drawn right
 *  on top of the data it was fit to, say -- the topmost one would otherwise
 *  permanently shadow everything underneath it, since it wins every click
 *  forever. Clicking the SAME spot again, on something already selected,
 *  steps to the next one down the stack instead of reselecting the same
 *  element every time (PowerPoint/Illustrator's alt-click-to-select-behind,
 *  minus the modifier key since plain re-click is unambiguous here). */
function pickClickTarget(evt) {
  const stack = [];
  const seen = new Set();
  for (const el of document.elementsFromPoint(evt.clientX, evt.clientY)) {
    const g = el.closest('g[id^="t_"]');
    if (g && !seen.has(g.id)) { seen.add(g.id); stack.push(g); }
  }
  if (!stack.length) return null;
  if (selection.length === 1 && stack.length > 1) {
    const idx = stack.findIndex((g) => g.id.slice(2) === selection[0]);
    if (idx !== -1) return stack[(idx + 1) % stack.length];
  }
  return stack[0];
}

canvas.addEventListener('pointerdown', (evt) => {
  const g = pickClickTarget(evt);
  if (g) {
    // A click on one tick label selects every tick on that axis as a
    // single group; resolve() already normalises the per-tick svg gid to
    // that group's canonical id, so reuse it here rather than letting two
    // different ticks pile up as separate selection entries. An arrow
    // endpoint's raw gid likewise resolves to its own arrow's id -- the
    // arrow is what gets selected either way.
    const rawId = g.id.slice(2);
    const resolved = resolve(rawId);
    const id = resolved?.id ?? rawId;
    // An endpoint handle only "activates" (drags just that point, changing
    // length and angle) once its arrow is already the sole selection;
    // otherwise the click just selects the whole arrow, same as clicking
    // its body -- you see the handles before you can grab one.
    const arrowAlreadySelected = selection.length === 1 && selection[0] === id;
    const pointKey = resolved?.pointKey && arrowAlreadySelected ? resolved.pointKey : null;

    if (isMulti(evt)) { toggleSelection(id); return; }
    // Clicking inside an existing multi-selection keeps it, so the whole group
    // can be dragged together.
    if (!selection.includes(id)) setSelection([id]);

    const movers = selected().filter((s) => s.draggable);
    if (!movers.length) return;

    evt.preventDefault();
    drag = {
      start: clientToSvg(evt),
      moved: false,
      // Where each label is *meant* to be right now, which may differ from
      // where the SVG draws it if a render is still in flight.
      items: movers.map((s) => {
        if (s.kind === 'arrow') {
          if (s.id === id && pointKey) {
            // This one arrow, grabbed by one endpoint: only that point moves.
            return { id: s.id, panel: s.panel, obj: s.obj, mode: pointKey,
                     baseSvg: dataToSvg(s.panel, s.obj[pointKey][0], s.obj[pointKey][1]) };
          }
          // Grabbed by its body (or swept up in a multi-selection): both
          // endpoints translate together, so the arrow just moves as a whole.
          return { id: s.id, panel: s.panel, obj: s.obj, mode: 'both',
                   baseP0: dataToSvg(s.panel, s.obj.p0[0], s.obj.p0[1]),
                   baseP1: dataToSvg(s.panel, s.obj.p1[0], s.obj.p1[1]) };
        }
        return { id: s.id, panel: s.panel, obj: s.obj, coords: s.coords ?? 'data',
                 baseSvg: dragBaseSvg(s) };
      }),
    };
    g.classList.add('ff-dragging');
    svgEl.setPointerCapture(evt.pointerId);
    return;
  }

  evt.preventDefault();
  rubber = {
    startClient: { x: evt.clientX, y: evt.clientY },
    moved: false,
    ctrl: isMulti(evt),
    baseSelection: [...selection],
    hitPanel: panelAt(clientToSvg(evt)),
    el: null,
  };
  svgEl.setPointerCapture(evt.pointerId);
});

canvas.addEventListener('pointermove', (evt) => {
  if (drag) {
    const now = clientToSvg(evt);
    const dx = now.x - drag.start.x;
    const dy = now.y - drag.start.y;
    if (!drag.moved && Math.hypot(dx, dy) < 1.5) return;
    if (!drag.moved) pushHistory();
    drag.moved = true;

    // Move the SPEC and let the preview machinery draw it. Going through the
    // SPEC rather than nudging the SVG directly means a drag composes with
    // any edit that hasn't been rendered yet.
    for (const it of drag.items) {
      const p = geometry.panels[it.panel];
      if (it.mode === 'both') {
        // Whole arrow: both endpoints shift by the identical delta, so the
        // shape and its length/angle are unchanged, just its position.
        const [nx0, ny0] = svgToData(it.panel, it.baseP0[0] + dx, it.baseP0[1] + dy);
        const [nx1, ny1] = svgToData(it.panel, it.baseP1[0] + dx, it.baseP1[1] + dy);
        it.obj.p0 = [roundTo(nx0, p.xlim), roundTo(ny0, p.ylim)];
        it.obj.p1 = [roundTo(nx1, p.xlim), roundTo(ny1, p.ylim)];
      } else if (it.mode === 'p0' || it.mode === 'p1') {
        // One endpoint, the other fixed: pulling it out lengthens the
        // arrow, swinging it around rotates it -- the same gesture does
        // both, same as dragging any line's endpoint handle.
        const [nx, ny] = svgToData(it.panel, it.baseSvg[0] + dx, it.baseSvg[1] + dy);
        it.obj[it.mode] = [roundTo(nx, p.xlim), roundTo(ny, p.ylim)];
      } else {
        const [nx, ny] = svgToData(it.panel, it.baseSvg[0] + dx, it.baseSvg[1] + dy, it.coords);
        const [limX, limY] = it.coords === 'axes'
          ? [AXES_FRAC_LIM, AXES_FRAC_LIM] : [p.xlim, p.ylim];
        it.obj.xy = [roundTo(nx, limX), roundTo(ny, limY)];
      }
    }
    showXY();
    reconcilePreviews();
    return;
  }

  if (rubber) {
    const dx = evt.clientX - rubber.startClient.x;
    const dy = evt.clientY - rubber.startClient.y;
    if (!rubber.moved && Math.hypot(dx, dy) < 3) return;
    rubber.moved = true;
    updateRubberBand(evt);
  }
});

canvas.addEventListener('pointerup', (evt) => {
  if (drag) {
    const moved = drag.moved;
    svgEl.querySelectorAll('.ff-dragging').forEach((n) => n.classList.remove('ff-dragging'));
    try { svgEl.releasePointerCapture(evt.pointerId); } catch { /* already gone */ }
    drag = null;
    if (moved) { drawOutlines(); scheduleSave(0); }
    return;
  }

  if (rubber) {
    try { svgEl.releasePointerCapture(evt.pointerId); } catch { /* already gone */ }
    if (rubber.moved) {
      rubber.el?.remove();                         // selection already live-set
    } else if (rubber.hitPanel) {
      const id = `panel:${rubber.hitPanel}`;
      if (rubber.ctrl) toggleSelection(id); else setSelection([id]);
    } else if (!rubber.ctrl) {
      setSelection([]);                            // click in the margin
    }
    rubber = null;
  }
});

canvas.addEventListener('pointercancel', () => {
  svgEl?.querySelectorAll('.ff-dragging').forEach((n) => n.classList.remove('ff-dragging'));
  drag = null;
  if (rubber) { rubber.el?.remove(); rubber = null; }
});

function showXY() {
  const sel = selected();
  // A legend still on its loc preset has xy: null until the first drag
  // commits one -- guard rather than assume, even though both call sites
  // currently run just after that write.
  if (sel.length === 1 && sel[0].draggable && sel[0].obj.xy) {
    $('f-x').value = sel[0].obj.xy[0];
    $('f-y').value = sel[0].obj.xy[1];
  }
}

/* ------------------------------------------------------ keyboard */

document.addEventListener('keydown', (evt) => {
  if (evt.key === 'Escape') { setSelection([]); return; }

  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);

  if ((evt.ctrlKey || evt.metaKey) && evt.key.toLowerCase() === 'z') {
    // While typing (e.g. retyping a label's text), Ctrl+Z should undo the
    // last keystroke in that field via the browser's own native text undo,
    // not jump out and revert the whole app-level history stack.
    if (typing) return;
    evt.preventDefault();
    undo();
    return;
  }

  // Ctrl+A / Ctrl+X / Ctrl+C / Ctrl+V are never intercepted here, so a
  // focused text field always gets the browser's native select-all / cut /
  // copy / paste -- nothing to wire up for those.
  if (typing || !selection.length) return;

  // PowerPoint's Ctrl+Shift+> / Ctrl+Shift+< grow/shrink the selected text.
  // evt.key is already the shifted character ('>'/'<') on a standard layout,
  // but some browsers report the unshifted key with shiftKey set instead --
  // check both so the shortcut isn't layout-fragile.
  const grow = evt.key === '>' || (evt.shiftKey && evt.key === '.');
  const shrink = evt.key === '<' || (evt.shiftKey && evt.key === ',');
  if (isMulti(evt) && (grow || shrink)) {
    evt.preventDefault();
    bumpSize(grow ? 1 : -1);
    return;
  }

  const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  const a = arrows[evt.key];
  if (!a) return;
  const movers = selected().filter((s) => s.draggable);
  if (!movers.length) return;

  evt.preventDefault();
  const step = evt.shiftKey ? 10 : 1;           // SVG units == points
  pushHistory();
  for (const s of movers) {
    const p = geometry.panels[s.panel];
    if (s.kind === 'arrow') {
      // Nudging moves the whole arrow, same as dragging its body.
      for (const key of ['p0', 'p1']) {
        const base = dataToSvg(s.panel, s.obj[key][0], s.obj[key][1]);
        const [nx, ny] = svgToData(s.panel, base[0] + a[0] * step, base[1] + a[1] * step);
        s.obj[key] = [roundTo(nx, p.xlim), roundTo(ny, p.ylim)];
      }
      continue;
    }
    const coords = s.coords ?? 'data';
    const base = dragBaseSvg(s);
    const [nx, ny] = svgToData(s.panel, base[0] + a[0] * step, base[1] + a[1] * step, coords);
    const [limX, limY] = coords === 'axes' ? [AXES_FRAC_LIM, AXES_FRAC_LIM] : [p.xlim, p.ylim];
    s.obj.xy = [roundTo(nx, limX), roundTo(ny, limY)];
  }
  showXY();
  reconcilePreviews();
  scheduleSave(250);
});

/* ------------------------------------------------------- field wiring */

$('f-text').addEventListener('input', (e) => {
  edit((o) => { o.text = e.target.value; }, { immediate: false });
});

// Greek letters / symbols insert at the cursor, matching how any real
// symbol picker behaves -- not just appended to the end. matplotlib
// mathtext renders literal Unicode Greek directly (confirmed against the
// actual renderer), so the inserted character needs no backslash-command
// translation and works identically inside or outside $...$ math mode.
$('symbol-palette').addEventListener('click', (e) => {
  const btn = e.target.closest('.symbol-btn');
  if (!btn) return;
  const ta = $('f-text');
  const ch = btn.dataset.ch;
  const start = ta.selectionStart ?? ta.value.length;
  const end = ta.selectionEnd ?? ta.value.length;
  ta.value = ta.value.slice(0, start) + ch + ta.value.slice(end);
  const pos = start + ch.length;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
});

function setSize(v) {
  if (!Number.isFinite(v) || v <= 0) return;
  edit((o) => { o.size = Math.round(Math.min(72, Math.max(4, v)) * 2) / 2; },
       { immediate: false });
}
$('f-size').addEventListener('input', (e) => setSize(parseFloat(e.target.value)));

/** A+ / A- bump every selected label, which is the point of multi-select:
 *  they may start at different sizes and should stay proportional. */
/** Which font-size field a selected thing's own "A+/A-" applies to.
 *  Free labels, titles and axis labels all share the ordinary text
 *  `size`; a tick-axis selection has no such field, just its own
 *  xtick_size/ytick_size; a whole-panel selection has neither -- there
 *  is no single "panel font" to bump. */
function fontSizeKey(s) {
  if (s.kind === 'xticks') return 'xtick_size';
  if (s.kind === 'yticks') return 'ytick_size';
  if (s.kind === 'panel') return null;
  return 'size';
}

/** Bump whatever is selected by `delta` pt -- the A+/A- buttons and
 *  Ctrl+Shift+>/< both call this. They may start at different sizes and
 *  should stay proportional, so this always adds/subtracts rather than
 *  setting an absolute value. */
function bumpSize(delta) {
  const sel = selected().filter((s) => fontSizeKey(s));
  if (!sel.length) return;
  pushHistory();
  for (const s of sel) {
    const key = fontSizeKey(s);
    const base = key === 'size' ? 12 : 11;
    s.obj[key] = Math.min(72, Math.max(4, (s.obj[key] ?? base) + delta));
  }
  refreshInspector();
  reconcilePreviews();
  scheduleSave(250);
}
$('f-size-up').onclick = () => bumpSize(1);
$('f-size-down').onclick = () => bumpSize(-1);

$('f-color').addEventListener('input', (e) => {
  $('f-color-hex').value = e.target.value;
  edit((o) => { o.color = e.target.value; }, { immediate: false });
});
$('f-color-hex').addEventListener('change', (e) => {
  const v = e.target.value.trim();
  if (!/^#[0-9a-f]{6}$/i.test(v)) { e.target.value = $('f-color').value; return; }
  $('f-color').value = v;
  edit((o) => { o.color = v; });
});
for (const [fid, idx] of [['f-x', 0], ['f-y', 1]]) {
  $(fid).addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v)) edit((o) => { o.xy[idx] = v; });
  });
}
$('f-ha').addEventListener('change', (e) => edit((o) => { o.ha = e.target.value; }));
$('f-va').addEventListener('change', (e) => edit((o) => { o.va = e.target.value; }));
$('f-box').addEventListener('change', (e) => {
  const on = e.target.checked;
  edit((o) => {
    if (on) o.bbox = { boxstyle: 'round,pad=0.25', fc: 'white', ec: '#999999', lw: 0.8 };
    else delete o.bbox;
  });
});

for (const [fid, lim, idx] of [
  ['f-xmin', 'xlim', 0], ['f-xmax', 'xlim', 1],
  ['f-ymin', 'ylim', 0], ['f-ymax', 'ylim', 1],
]) {
  $(fid).addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v)) edit((o) => { o[lim][idx] = v; });
  });
}

/** Log scale needs strictly positive limits; matplotlib throws otherwise.
 *  Checked client-side so the field snaps back with an explanation instead
 *  of round-tripping to the server for a 422. */
function trySetScale(axis, value) {
  const key = axis === 'x' ? 'xlim' : 'ylim';
  if (value === 'log') {
    const bad = selected().find((s) => s.obj[key].some((v) => v <= 0));
    if (bad) {
      const note = $('axes-note');
      note.hidden = false;
      note.textContent =
        `Can't use log scale on the ${axis}-axis: panel (${bad.panel})'s range ` +
        `includes zero or a negative value.`;
      $(`f-${axis}scale`).value = 'linear';
      return;
    }
  }
  $('axes-note').hidden = true;
  edit((o) => { o[axis === 'x' ? 'xscale' : 'yscale'] = value; });
}
$('f-xscale').addEventListener('change', (e) => trySetScale('x', e.target.value));
$('f-yscale').addEventListener('change', (e) => trySetScale('y', e.target.value));
$('f-aspect').addEventListener('change', (e) => edit((o) => { o.aspect = e.target.value; }));

for (const [fid, key] of [['f-xticksize', 'xtick_size'], ['f-yticksize', 'ytick_size']]) {
  $(fid).addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v)) edit((o) => { o[key] = v; });
  });
}
$('f-framewidth').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v >= 0) edit((o) => { o.frame_lw = v; });
});

$('f-legend-size').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v > 0) edit((o) => { o.size = v; });
});
$('f-legend-frame').addEventListener('change', (e) => {
  const on = e.target.checked;
  edit((o) => { o.frameon = on; });
});
$('btn-legend-reset').addEventListener('click', () => {
  edit((o) => { delete o.xy; });
});

$('f-series-label').addEventListener('input', (e) => {
  edit((o) => { o.label = e.target.value; }, { immediate: false });
});
$('f-series-color').addEventListener('input', (e) => {
  $('f-series-color-hex').value = e.target.value;
  edit((o) => { (o.style ??= {}).color = e.target.value; }, { immediate: false });
});
$('f-series-color-hex').addEventListener('change', (e) => {
  const v = e.target.value.trim();
  if (!/^#[0-9a-f]{6}$/i.test(v)) { e.target.value = $('f-series-color').value; return; }
  $('f-series-color').value = v;
  edit((o) => { (o.style ??= {}).color = v; });
});
$('f-series-marker').addEventListener('change', (e) => {
  edit((o) => { (o.style ??= {}).marker = e.target.value; });
});
$('f-series-lw').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v >= 0) edit((o) => { (o.style ??= {}).lw = v; });
});
$('f-series-ms').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v >= 0) edit((o) => { (o.style ??= {}).ms = v; });
});
$('f-series-alpha').addEventListener('input', (e) => {
  const v = parseFloat(e.target.value);
  $('f-series-alpha-num').value = v;
  if (Number.isFinite(v)) edit((o) => { (o.style ??= {}).alpha = v; }, { immediate: false });
});
$('f-series-alpha-num').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v >= 0 && v <= 1) {
    $('f-series-alpha').value = v;
    edit((o) => { (o.style ??= {}).alpha = v; });
  }
});

$('f-arrow-color').addEventListener('input', (e) => {
  $('f-arrow-color-hex').value = e.target.value;
  edit((o) => { o.color = e.target.value; }, { immediate: false });
});
$('f-arrow-color-hex').addEventListener('change', (e) => {
  const v = e.target.value.trim();
  if (!/^#[0-9a-f]{6}$/i.test(v)) { e.target.value = $('f-arrow-color').value; return; }
  $('f-arrow-color').value = v;
  edit((o) => { o.color = v; });
});
$('f-arrow-style').addEventListener('change', (e) => {
  edit((o) => { o.arrowstyle = e.target.value; });
});
$('f-arrow-lw').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v >= 0) edit((o) => { o.lw = v; });
});
$('f-arrow-scale').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v > 0) edit((o) => { o.mutation_scale = v; });
});

/* ------------------------------------------------------------ toolbar */

function undo() {
  if (!history.length) return;
  spec = history.pop();
  $('btn-undo').disabled = !history.length;
  setSelection(selection);
  reconcilePreviews();
  scheduleSave(0);
}
$('btn-undo').onclick = undo;

async function download(url, filename) {
  setStatus('preparing…', 'busy');
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
    setStatus(`downloaded ${filename}`);
  } catch (e) {
    setStatus(e.message, 'err');
  }
}

$('btn-py').onclick = () => download(`/api/code/${FIGURE}`, `${FIGURE}.py`);
$('btn-png').onclick = () => download(`/api/png/${FIGURE}`, `${FIGURE}.png`);

// Two-step confirm rather than a modal dialog.
let rebuildArmed = false;
$('btn-rebuild').onclick = async () => {
  const btn = $('btn-rebuild');
  if (!rebuildArmed) {
    rebuildArmed = true;
    btn.textContent = 'Discard edits?';
    setTimeout(() => {
      if (rebuildArmed) { rebuildArmed = false; btn.textContent = 'Rebuild'; }
    }, 4000);
    return;
  }
  rebuildArmed = false;
  btn.textContent = 'Rebuild';
  setStatus('rebuilding from CSV…', 'busy');
  try {
    const r = await fetch(`/api/rebuild/${FIGURE}`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    history = [];
    $('btn-undo').disabled = true;
    spec = data.spec;
    renderedSpec = clone(data.spec);
    geometry = data.geometry;
    setSelection([]);
    applySvg(data.svg);
    buildList();
    setStatus(`rebuilt · rev ${spec.rev}`);
  } catch (e) {
    setStatus(e.message, 'err');
  }
};

$('zoom').addEventListener('input', (e) => {
  zoom = parseInt(e.target.value, 10);
  $('zoomval').textContent = zoom + '%';
  applyZoom();
});

$('btn-fit').onclick = fit;

function fit() {
  const w = $('canvas-wrap').clientWidth - 40;
  zoom = Math.max(40, Math.min(220, Math.round(w / geometry.width * 100)));
  $('zoom').value = zoom;
  $('zoomval').textContent = zoom + '%';
  applyZoom();
}

/* --------------------------------------------------------------- boot */

(async function init() {
  setStatus('loading…', 'busy');
  try {
    const r = await fetch(`/api/figure/${FIGURE}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    spec = data.spec;
    renderedSpec = clone(data.spec);
    geometry = data.geometry;
    $('figname').textContent = `${FIGURE}/spec.json`;
    $('btn-undo').disabled = true;
    applySvg(data.svg);
    buildList();
    fit();
    setStatus(`rev ${spec.rev}`);
  } catch (e) {
    setStatus(e.message, 'err');
    canvas.innerHTML = `<div style="padding:40px;font:14px sans-serif;color:#b4392b">
      Could not load the figure: ${escapeHtml(e.message)}<br><br>
      Have you run <code>python build.py</code>?</div>`;
  }
})();
