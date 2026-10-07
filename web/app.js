/* FigForge editor — click, retype, resize, recolor and drag figure labels.
 *
 * The SPEC is the source of truth. Every interaction does the same thing:
 * mutate the SPEC, POST it, and swap in the SVG matplotlib renders back.
 *
 * A matplotlib re-render takes ~0.8 s, far too slow to sit in the interaction
 * loop, so it isn't in it: edits are faked in the SVG immediately and the real
 * render replaces the fake when it lands. See reconcilePreviews().
 *
 * No AI, no external calls. Everything goes to the FigForge server that
 * served this page: 127.0.0.1 locally, or its own Vercel function when hosted.
 */

/** The open project (a figure folder name). Chosen at boot from ?project=,
 *  then the last one opened, then the first that exists. */
let FIGURE = null;
const LAST_PROJECT_KEY = 'figforge.lastProject';

let spec = null;          // live, authoritative
let renderedSpec = null;  // what the SVG currently on screen was rendered from
let geometry = null;
let svgEl = null;
let selection = [];       // element ids; selection[0] is the primary
let history = [];         // undo stack: past specs, newest last
let future = [];          // redo stack: specs undone, newest last
let zoom = 100;

const HISTORY_MAX = 100;  // per stack; the server enforces the same cap

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

/** Snapshot the spec before an edit. A new edit discards the redo stack,
 *  as in any editor. `coalesceKey` merges a burst of edits into one undo
 *  step: typing a label's text shouldn't take one Ctrl+Z per keystroke. */
let lastCoalesce = null;

function pushHistory(coalesceKey = null) {
  const now = Date.now();
  if (coalesceKey && lastCoalesce?.key === coalesceKey && now - lastCoalesce.t < 1000) {
    lastCoalesce.t = now;
    return;
  }
  lastCoalesce = coalesceKey ? { key: coalesceKey, t: now } : null;
  history.push(clone(spec));
  if (history.length > HISTORY_MAX) history.shift();
  future = [];
  refreshUndoButtons();
  scheduleHistorySave();
}

function refreshUndoButtons() {
  $('btn-undo').disabled = !history.length;
  $('btn-redo').disabled = !future.length;
}

/* The undo stack is persisted per project (history.json) so undo still works
 * after closing the browser. Debounced: it's up to ~1 MB, and a burst of
 * nudges shouldn't write it once per keypress. */
let historyTimer = null, historyPending = false;

function scheduleHistorySave() {
  historyPending = true;
  clearTimeout(historyTimer);
  historyTimer = setTimeout(saveHistoryNow, 800);
}

async function saveHistoryNow() {
  clearTimeout(historyTimer);
  if (!historyPending) return;
  historyPending = false;
  try {
    // Gzipped on the way up: 100 undo + 100 redo specs is ~4 MB of JSON,
    // right at the hosted (Vercel) request-size cap. It compresses ~20x.
    const json = JSON.stringify({ undo: history, redo: future });
    const headers = { 'Content-Type': 'application/json' };
    let body = json;
    if (typeof CompressionStream !== 'undefined') {
      body = await new Response(new Blob([json]).stream()
        .pipeThrough(new CompressionStream('gzip'))).blob();
      headers['X-Body-Encoding'] = 'gzip';
    }
    await fetch(`/api/history/${FIGURE}`, { method: 'POST', headers, body });
  } catch (e) {
    // Losing undo history is not worth interrupting editing over.
    console.warn('history save failed', e);
  }
}

let inFlight = false, needsSave = false, saveTimer = null, savePending = false;

function scheduleSave(delay = 0) {
  clearTimeout(saveTimer);
  savePending = true;
  saveTimer = setTimeout(doSave, delay);
}

/** Resolve once every edit made so far is on disk -- before leaving this
 *  project for another one, so a switch never drops the last change. */
async function flushPending() {
  while (savePending || inFlight || needsSave) {
    await new Promise((r) => setTimeout(r, 50));
  }
  await saveHistoryNow();
}

async function doSave() {
  savePending = false;
  if (inFlight) { needsSave = true; return; }
  inFlight = true;
  setStatus('Saving…', 'busy');
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
    buildList();
    setStatus('Saved ✓');
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
  const m = id.match(/^(.+)__(title|xlabel|ylabel|top_axis|right_axis)$/);
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
  // A colour bar belongs to its heatmap / contour / scatter.
  const cbar = id.match(/^(.+)__cbar$/);
  if (cbar) return resolve(cbar[1], source);
  // The grey lines through zero: one element per panel, drawn as two
  // paths ("__zero__h" / "__zero__v" in the SVG).
  const zeroMatch = id.match(/^(.+)__zero(?:__[hv])?$/);
  if (zeroMatch) {
    const p = source.panels.find((q) => q.id === zeroMatch[1]);
    if (!p?.zero_lines) return null;
    return { id: `${p.id}__zero`, kind: 'zero', obj: p, draggable: false, panel: p.id };
  }
  // Guide lines (axhline/axvline) have no stored id, so they're named by
  // their place in the panel's hlines/vlines list.
  const guideMatch = id.match(/^(.+)__(h|v)line_(\d+)$/);
  if (guideMatch) {
    const [, pid, hv, i] = guideMatch;
    const line = source.panels.find((q) => q.id === pid)?.[`${hv}lines`]?.[+i];
    if (!line) return null;
    return { id, kind: 'guide', obj: line, draggable: false, panel: pid, axis: hv };
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
    for (const k of ['title', 'xlabel', 'ylabel', 'top_axis', 'right_axis']) {
      if (p[k]?.text) out.push(resolve(`${p.id}__${k}`, source));
    }
    out.push(resolve(`${p.id}__legend`, source));
    for (const t of p.texts || []) out.push(resolve(t.id, source));
    for (const sr of p.series || []) out.push(resolve(sr.id, source));
    for (const a of p.arrows || []) out.push(resolve(a.id, source));
    for (const hv of ['h', 'v']) {
      (p[`${hv}lines`] || []).forEach((_, i) => out.push(resolve(`${p.id}__${hv}line_${i}`, source)));
    }
    out.push(resolve(`${p.id}__zero`, source));
  }
  return out.filter(Boolean);
}

/* What each kind of element is called on screen. Plain words, no jargon:
 * "side numbers", not "y-axis tick labels". */
const KIND_LABEL = {
  suptitle: 'figure title', title: 'title', panel: 'plot box',
  xlabel: 'bottom label', ylabel: 'side label', text: 'label',
  top_axis: 'top axis label', right_axis: 'right axis label',
  xticks: 'bottom numbers', yticks: 'side numbers', legend: 'legend',
  series: 'line', arrow: 'arrow', guide: 'guide line', zero: 'zero lines',
};
const KIND_PLURAL = {
  suptitle: 'figure titles', title: 'titles', panel: 'plot boxes',
  xlabel: 'bottom labels', ylabel: 'side labels', text: 'labels',
  top_axis: 'top axis labels', right_axis: 'right axis labels',
  xticks: 'bottom numbers', yticks: 'side numbers', legend: 'legends',
  series: 'lines', arrow: 'arrows', guide: 'guide lines', zero: 'zero lines',
};

const plotName = (pid) => `plot (${pid})`;

/** KIND_LABEL, except a curve drawn only as markers is called "dots". */
/* A curve's own kind (layers.py): what it's called on screen. */
const SERIES_WORDS = {
  errorbar: 'error bars', band: 'band', bar: 'bars', step: 'steps', scatter: 'coloured dots',
  heatmap: 'heatmap', contour: 'contour lines', contourf: 'filled contours', box: 'box plot',
};
const CMAP_KINDS = new Set(['heatmap', 'contour', 'contourf', 'scatter']);

function kindLabel(kind, obj) {
  if (kind === 'series' && obj?.kind && SERIES_WORDS[obj.kind]) return SERIES_WORDS[obj.kind];
  if (kind === 'series' && obj && !seriesHasLine(obj.style)) return 'dots';
  return KIND_LABEL[kind];
}
const seriesHasLine = (st = {}) =>
  !['none', 'None', '', ' '].includes(st.ls ?? st.linestyle ?? '-');
const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1);

/* Panels and tick labels are structure, not items -- deleting one would
 * mean re-laying-out the figure, so they're refused rather than guessed at.
 * Titles and axis labels are blanked, not removed, so their size/color
 * survive if the text is ever typed back. */
const DELETABLE = new Set(['text', 'arrow', 'series', 'legend', 'guide', 'zero', 'top_axis', 'right_axis',
                           'title', 'xlabel', 'ylabel', 'suptitle']);

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
    if (!['series', 'arrow', 'guide', 'zero'].includes(r.kind)) addHitTarget(g);
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
    if (!g) {
      svgPartsFor(id).forEach((part) => {
        let pb;
        try { pb = part.getBBox(); } catch { return; }
        svgEl.appendChild(mkOutlineRect(pb.x - 3, pb.y - 3, pb.width + 6, pb.height + 6, i === 0));
      });
      return;
    }
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

/** Every svg group drawn for one element: its own gid plus any "<gid>__*"
 *  parts (arrow endpoints and visible body). An exact/prefix match rather
 *  than a bare prefix, so "a_R" never catches "a_R_arrow". */
function svgPartsFor(id) {
  const gid = CSS.escape('t_' + id);
  return svgEl.querySelectorAll(`[id="${gid}"], [id^="${gid}__"]`);
}

function reconcilePreviews() {
  if (!renderedSpec || !svgEl) return;

  // Deleted since this SVG was rendered: hide it now, the real render drops
  // it. Also un-hides it again if an undo brings it back before that lands.
  // (A dense curve's rasterized pixels carry no gid, so those alone wait
  // for the real render.)
  const live = new Set(allElements(spec).map((e) => e.id));
  for (const el of allElements(renderedSpec)) {
    const gone = !live.has(el.id);
    svgPartsFor(el.id).forEach((g) => { g.style.display = gone ? 'none' : ''; });
  }

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
  if (!sel.length) refreshFigureFields();
  $('insp-body').hidden = sel.length === 0;
  refreshDataPanel(sel);
  if (!sel.length) return;

  const multi = sel.length > 1;
  const kinds = [...new Set(sel.map((s) => s.kind))];
  if (multi) {
    $('insp-kind').textContent = kinds.length === 1
      ? `${sel.length} ${KIND_PLURAL[kinds[0]]}`
      : `${sel.length} things`;
    $('insp-where').textContent = 'picked';
  } else {
    $('insp-kind').textContent = capitalize(kindLabel(sel[0].kind, sel[0].obj));
    $('insp-where').textContent = sel[0].panel ? `in ${plotName(sel[0].panel)}` : '';
  }
  $('btn-delete').hidden = !sel.some((s) => DELETABLE.has(s.kind));

  const allPanels = sel.every((s) => s.kind === 'panel'
    || s.kind === 'xticks' || s.kind === 'yticks');
  const allLegends = sel.every((s) => s.kind === 'legend');
  const allSeries = sel.every((s) => s.kind === 'series');
  const allArrows = sel.every((s) => s.kind === 'arrow');
  const allGuides = sel.every((s) => s.kind === 'guide');
  const allZero = sel.every((s) => s.kind === 'zero');
  $('label-fields').hidden = allPanels || allLegends || allSeries || allArrows
    || allGuides || allZero;
  $('guide-fields').hidden = !allGuides;
  $('zero-fields').hidden = !allZero;
  $('axes-fields').hidden = !allPanels;
  $('legend-fields').hidden = !allLegends;
  $('series-fields').hidden = !allSeries;
  $('arrow-fields').hidden = !allArrows;
  if (allPanels) { refreshAxesInspector(); return; }
  if (allLegends) { refreshLegendInspector(); return; }
  if (allSeries) { refreshSeriesInspector(); return; }
  if (allArrows) { refreshArrowInspector(); return; }
  if (allGuides) { refreshGuideInspector(); return; }
  if (allZero) return;

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
    $('f-xunit').innerHTML = p.xlabel?.text ? `(${mathHtml(p.xlabel.text)})` : '';
    $('f-yunit').innerHTML = p.ylabel?.text ? `(${mathHtml(p.ylabel.text)})` : '';
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
    // Box settings apply to whichever selected labels have a box, so a
    // mixed selection ("All labels") can still tighten every box at once.
    const withBox = sel.filter((s) => s.obj.bbox);
    $('box-colors').hidden = !withBox.length;
    if (withBox.length) {
      const same = (get) => {
        const vals = withBox.map(get);
        return vals.every((v) => v === vals[0]) ? vals[0] : undefined;
      };
      showBoxColor('f-box-fc', same((s) => boxColorName(s.obj.bbox.fc)));
      showBoxColor('f-box-ec', same((s) => boxColorName(s.obj.bbox.ec)));
      const pad = same((s) => boxPad(s.obj.bbox));
      $('f-box-pad').value = pad ?? '';
      $('f-box-pad').placeholder = pad === undefined ? 'mixed' : '';
    }
  }
}

/* Box fill/border offer only white and black for now. Labels built with
 * another color (the orange-bordered β boxes) keep it until changed; the
 * select then shows that color, or "mixed", as a placeholder. */
/* Padding lives inside matplotlib's boxstyle string ("round,pad=0.25"),
 * in units of the label's font size. */
const DEFAULT_BOX_PAD = 0.1;

function boxPad(bbox) {
  const m = String(bbox?.boxstyle ?? '').match(/pad=([\d.]+)/);
  return m ? parseFloat(m[1]) : 0.3;  // 0.3 is matplotlib's own default
}

function withPad(boxstyle, pad) {
  const style = String(boxstyle ?? 'round');
  return /pad=/.test(style)
    ? style.replace(/pad=[\d.]+/, `pad=${pad}`)
    : `${style},pad=${pad}`;
}

const BOX_COLORS = { white: ['white', '#fff', '#ffffff'], black: ['black', '#000', '#000000'] };

function boxColorName(c) {
  const v = String(c ?? '').toLowerCase();
  return Object.keys(BOX_COLORS).find((k) => BOX_COLORS[k].includes(v)) ?? v;
}

function showBoxColor(fid, value) {
  const sel = $(fid);
  sel.querySelector('option.placeholder')?.remove();
  if (value in BOX_COLORS) { sel.value = value; return; }
  const o = document.createElement('option');
  o.className = 'placeholder';
  o.disabled = true;
  o.textContent = value === undefined ? 'mixed' : value;
  sel.prepend(o);
  sel.selectedIndex = 0;
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
  const zero = common((s) => !!s.obj.zero_lines);
  $('f-zero').indeterminate = zero === undefined;
  $('f-zero').checked = zero === true;
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
function refreshCmapFields() {
  const st = (s) => s.obj.style || {};
  $('f-cmap').value = common((s) => st(s).cmap ?? 'viridis') ?? 'viridis';
  $('f-cmap-log').checked = common((s) => st(s).norm === 'log') === true;
  const lo = common((s) => st(s).vmin ?? null), hi = common((s) => st(s).vmax ?? null);
  $('f-cmap-min').value = lo ?? '';
  $('f-cmap-max').value = hi ?? '';
  const bar = common((s) => !!s.obj.colorbar);
  $('f-cbar').checked = bar === true;
  $('f-cbar-label-field').hidden = bar !== true;
  $('f-cbar-label').value = common((s) => s.obj.colorbar?.label ?? '') ?? '';
}

function refreshSeriesInspector() {
  const label = common((s) => s.obj.label ?? '');
  $('f-series-label').value = label ?? '';
  $('f-series-label').placeholder = label === undefined ? 'mixed' : 'No name';

  const color = common((s) => normHex(s.obj.style?.color ?? '#000000'));
  $('f-series-color').value = color ?? '#000000';
  $('f-series-color-hex').value = color ?? '';
  $('f-series-color-hex').placeholder = color === undefined ? 'mixed' : '';

  $('f-series-marker').value = common((s) => s.obj.style?.marker ?? 'none') ?? 'none';

  // Show only the settings this kind of curve has.
  const kinds = new Set(selected().map((s) => s.obj.kind || 'line'));
  const all = (...ks) => [...kinds].every((k) => ks.includes(k));
  const noDots = all('band', 'bar', 'heatmap', 'contour', 'contourf', 'box');
  $('f-series-marker').closest('label').hidden = noDots;
  $('f-series-ms').closest('label').hidden = noDots || all('scatter');
  $('f-series-lw').closest('label').hidden = noDots || all('scatter');
  const mapped = selected().every((s) => CMAP_KINDS.has(s.obj.kind) && (s.obj.kind !== 'scatter' || s.obj.c));
  $('f-series-color').closest('label').hidden = mapped;
  $('cmap-fields').hidden = !mapped;
  if (mapped) refreshCmapFields();

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
function refreshGuideInspector() {
  const color = common((s) => normHex(s.obj.color ?? '0.8'));
  $('f-guide-color').value = color ?? '#cccccc';
  $('f-guide-color-hex').value = color ?? '';
  $('f-guide-color-hex').placeholder = color === undefined ? 'mixed' : '';
  $('f-guide-ls').value = common((s) => s.obj.ls ?? '-') ?? '-';
  const lw = common((s) => s.obj.lw ?? 1.0);
  $('f-guide-lw').value = lw ?? '';
  $('f-guide-lw').placeholder = lw === undefined ? 'mixed' : '';

  // Where it sits only makes sense for one line.
  const one = selected().length === 1 ? selected()[0] : null;
  $('guide-pos-field').hidden = !one;
  if (one) {
    const p = panelById(one.panel);
    const label = one.axis === 'v' ? p.xlabel?.text : p.ylabel?.text;
    $('f-guide-pos-name').textContent = one.axis === 'v' ? 'Across at' : 'Up at';
    $('f-guide-pos-unit').innerHTML = label ? `(${mathHtml(label)})` : '';
    $('f-guide-pos').value = one.obj[one.axis === 'v' ? 'x' : 'y'];
  }
}

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

/* The numbers behind a picked curve, editable: click a number, type,
 * Enter. The server sends the numbers as stored; the user's changes live in
 * spec.data_edits ({array key: {row: value}}) and are laid on top here, so
 * undo/redo show at once. Numbers written inline in the spec (no array key)
 * are changed in the spec itself. */
let dataPanel = null;   // {s, cols} for the curve on show

function refreshDataPanel(sel) {
  const panelEl = $('data-panel');
  const single = sel.length === 1 && sel[0].kind === 'series';
  panelEl.hidden = !single;
  if (!single) { dataPanel = null; return; }

  const s = sel[0];
  const p = panelById(s.panel);
  $('data-title').innerHTML = s.obj.label ? mathHtml(s.obj.label)
    : `${capitalize(kindLabel('series', s.obj))} with no name`;
  if (dataPanel?.s.id === s.id && dataPanel.cols) {
    dataPanel.s = s;
    drawDataTable(p);           // same curve: just re-lay the edits
    return;
  }
  $('data-count').textContent = '';
  $('data-status').textContent = 'Loading…';
  $('data-status').className = 'data-status';
  $('data-table-body').innerHTML = '';
  dataPanel = { s, cols: null };

  const token = ++dataPanelToken;
  fetch(`/api/data/${FIGURE}/${s.id}`)
    .then((r) => r.json().then((data) => ({ ok: r.ok, data })))
    .then(({ ok, data }) => {
      if (token !== dataPanelToken) return;  // a newer selection fired since
      if (!ok) throw new Error(data.error || 'failed to load');
      $('data-status').textContent = '';
      dataPanel = { s, cols: data.columns, note: data.note };
      drawDataTable(p);
    })
    .catch((e) => {
      if (token !== dataPanelToken) return;
      $('data-status').textContent = e.message;
      $('data-status').className = 'data-status err';
    });
}

/** The value on show for one cell: the user's change if there is one. */
function dataValue(col, i) {
  if (col.key) {
    const v = spec.data_edits?.[col.key]?.[i];
    if (v !== undefined) return { v, edited: true };
    return { v: col.values[i], edited: false };
  }
  const own = dataPanel.s.obj[col.field];
  return { v: Array.isArray(own) ? own[i] : col.values[i], edited: false };
}

function drawDataTable(p) {
  const { cols } = dataPanel;
  const titles = {
    x: p.xlabel?.text ? mathHtml(p.xlabel.text) : 'Across (x)',
    y: p.ylabel?.text ? mathHtml(p.ylabel.text) : 'Up (y)',
  };
  $('data-head-row').innerHTML = cols.map((c) => `<th>${titles[c.field] ?? escapeHtml(c.title)}</th>`).join('');
  if (!cols.length) {
    $('data-count').textContent = dataPanel.note || '';
    $('data-table-body').innerHTML = '';
    return;
  }
  const n = Math.max(...cols.map((c) => c.values.length));
  $('data-count').textContent = `${n.toLocaleString()} point${n === 1 ? '' : 's'} · click a number to change it`;
  const rows = [];
  for (let i = 0; i < n; i++) {
    rows.push('<tr>' + cols.map((c, j) => {
      if (i >= c.values.length) return '<td class="empty"></td>';
      const { v, edited } = dataValue(c, i);
      return `<td data-i="${i}" data-c="${j}"${edited ? ' class="edited" title="Changed by hand"' : ''}>` +
        `${v === null || v === undefined ? '' : fmtNum(v)}</td>`;
    }).join('') + '</tr>');
  }
  $('data-table-body').innerHTML = rows.join('');
}

$('data-table-body').addEventListener('click', (e) => {
  const td = e.target.closest('td');
  if (!td || td.classList.contains('empty') || td.querySelector('input') || !dataPanel?.cols) return;
  const i = +td.dataset.i, col = dataPanel.cols[+td.dataset.c];
  const { v } = dataValue(col, i);
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'data-edit';
  input.value = v === null || v === undefined ? '' : String(v);
  td.textContent = '';
  td.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const text = input.value.trim().replace(/,/g, '');
    const num = Number(text);
    if (save && text !== '' && Number.isFinite(num) && text !== fmtNum(v)) {
      // Typing back what the table showed for the original (it's rounded
      // there) means "the original", not a new, slightly different value.
      const orig = col.values[i];
      setDataValue(col, i, orig !== null && text === fmtNum(orig) ? orig : num);
    }
    drawDataTable(panelById(dataPanel.s.panel));
  };
  input.addEventListener('keydown', (ev) => {
    ev.stopPropagation();                 // arrows/Delete edit the number, not the figure
    if (ev.key === 'Enter') finish(true);
    if (ev.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
});

function setDataValue(col, i, num) {
  pushHistory();
  if (col.key) {
    spec.data_edits ??= {};
    spec.data_edits[col.key] ??= {};
    if (num === col.values[i]) delete spec.data_edits[col.key][i];   // back to the original
    else spec.data_edits[col.key][i] = num;
    if (!Object.keys(spec.data_edits[col.key]).length) delete spec.data_edits[col.key];
    if (!Object.keys(spec.data_edits).length) delete spec.data_edits;
  } else {
    dataPanel.s.obj[col.field][i] = num;
  }
  setStatus('Saving…', 'busy');
  scheduleSave(0);
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

/* ------------------------------------------- mathtext, made readable
 *
 * Labels are stored as matplotlib mathtext ("Re $S_{11}$", "$\beta_1=0.53$").
 * The figure renders it properly, but the editor's lists and field labels
 * are plain HTML -- showing the raw source there is unreadable. mathHtml()
 * turns it into escaped HTML with real Greek letters and <sub>/<sup>. It is
 * display-only: the stored text is never touched.
 */
const MATH_SYMBOLS = {
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', varepsilon: 'ε',
  zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ', iota: 'ι', kappa: 'κ',
  lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ',
  tau: 'τ', upsilon: 'υ', phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
  Sigma: 'Σ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  pm: '±', mp: '∓', times: '×', cdot: '·', div: '÷', circ: '°', degree: '°',
  infty: '∞', approx: '≈', sim: '~', simeq: '≃', neq: '≠', ne: '≠',
  leq: '≤', le: '≤', geq: '≥', ge: '≥', ll: '≪', gg: '≫', propto: '∝',
  partial: '∂', nabla: '∇', sqrt: '√', hbar: 'ħ', ell: 'ℓ', AA: 'Å',
  to: '→', rightarrow: '→', leftarrow: '←', leftrightarrow: '↔',
  uparrow: '↑', downarrow: '↓', updownarrow: '↕', Rightarrow: '⇒', Leftarrow: '⇐',
  Uparrow: '⇑', Downarrow: '⇓', nearrow: '↗', searrow: '↘', swarrow: '↙', nwarrow: '↖',
  star: '⋆', bullet: '•', dagger: '†', checkmark: '✓', cdots: '⋯', ldots: '…', dots: '…',
  langle: '⟨', rangle: '⟩', prime: '′', perp: '⊥', parallel: '∥',
  ',': ' ', ';': ' ', ':': ' ', '!': '', quad: ' ', qquad: '  ', ' ': ' ',
  '%': '%', '$': '$', '{': '{', '}': '}', '_': '_', '#': '#', '&': '&',
};

function mathHtml(s) {
  const parts = String(s ?? '').replace(/\n/g, ' ').split('$');
  // Even parts are plain text, odd parts are inside $...$ (an unmatched
  // trailing $ leaves plain text, as matplotlib would show it).
  if (parts.length % 2 === 0) parts[parts.length - 2] += '$' + parts.pop();
  return parts.map((p, i) => (i % 2 ? mathPart(p) : escapeHtml(p))).join('');
}

function mathPart(src) {
  let i = 0;
  const group = () => {           // after '{': read to the matching '}'
    let out = '';
    while (i < src.length && src[i] !== '}') out += atom();
    i++;
    return out;
  };
  const arg = () => {             // what _ ^ and \cmd apply to
    while (src[i] === ' ') i++;
    if (src[i] === '{') { i++; return group(); }
    return i < src.length ? atom() : '';
  };
  const atom = () => {
    const c = src[i++];
    if (c === '{') return group();
    if (c === '_') return `<sub>${arg()}</sub>`;
    if (c === '^') return `<sup>${arg()}</sup>`;
    if (c === ' ') return '';     // mathtext ignores spaces
    if (c !== '\\') return escapeHtml(c);
    const name = /^[a-zA-Z]+/.exec(src.slice(i))?.[0] ?? src[i] ?? '';
    i += name.length;
    if (name in MATH_SYMBOLS) return MATH_SYMBOLS[name];
    // Font switches and \frac-like wrappers: just show what's inside.
    if (/^(math[a-z]+|text[a-z]*|rm|it|bf|operatorname|mathrm|boldsymbol|bar|hat|tilde|vec|dot|overline)$/.test(name)) {
      return arg();
    }
    if (name === 'frac') { const a = arg(); return `${a}/${arg()}`; }
    return escapeHtml(name);
  };
  let out = '';
  while (i < src.length) out += atom();
  return out;
}

/** Apply an edit to every selected element, preview it, then persist. */
function edit(fn, { immediate = true } = {}) {
  const sel = selected();
  if (!sel.length) return;
  // Continuous edits (typing, spinning a number) collapse into one step.
  pushHistory(immediate ? null : 'edit:' + selection.join(','));
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
    add('All plot boxes', spec.panels.map((p) => resolve(`panel:${p.id}`)).filter(Boolean));
    return groups;
  }

  if (primary.kind === 'xticks' || primary.kind === 'yticks') {
    const axis = primary.kind === 'xticks' ? 'x' : 'y';
    add(`All ${KIND_LABEL[primary.kind]}`, spec.panels
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
    add(`All lines in ${plotName(primary.panel)}`,
        all.filter((e) => e.kind === 'series' && e.panel === primary.panel));
    add('All lines', all.filter((e) => e.kind === 'series'));
    return groups;
  }

  if (primary.kind === 'guide') {
    add(`All guide lines in ${plotName(primary.panel)}`,
        all.filter((e) => e.kind === 'guide' && e.panel === primary.panel));
    add('All guide lines', all.filter((e) => e.kind === 'guide'));
    return groups;
  }
  if (primary.kind === 'zero') {
    add('All zero lines', all.filter((e) => e.kind === 'zero'));
    return groups;
  }

  if (primary.kind === 'arrow') {
    // Dedicated too: an arrow has no .size (it has mutation_scale/lw
    // instead), so the generic "Everything at Npt" bucket below would
    // compare against a field that doesn't exist.
    add(`All arrows in ${plotName(primary.panel)}`,
        all.filter((e) => e.kind === 'arrow' && e.panel === primary.panel));
    add('All arrows', all.filter((e) => e.kind === 'arrow'));
    return groups;
  }

  if (primary.kind === 'text') {
    add(`All labels in ${plotName(primary.panel)}`,
        all.filter((e) => e.kind === 'text' && e.panel === primary.panel));
    add('All labels', all.filter((e) => e.kind === 'text'));
  } else if (primary.kind !== 'suptitle') {
    add(`All ${KIND_PLURAL[primary.kind]}`,
        all.filter((e) => e.kind === primary.kind));
  }

  const size = primary.obj.size ?? 12;
  add(`Everything this size (${size} pt)`, all.filter((e) => (e.obj.size ?? 12) === size));

  const color = normHex(primary.obj.color ?? '#000000');
  const sameColor = all.filter((e) => normHex(e.obj.color ?? '#000000') === color);
  if (sameColor.length < all.length) {
    add(`<i class="swatch" style="background:${color}"></i>Everything this color`, sameColor);
  }

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
    b.innerHTML = `${g.label}<span class="n">${g.ids.length}</span>`;
    b.title = 'Click to select this group · Ctrl-click to add it';
    b.onclick = (evt) => setSelection(
      isMulti(evt) ? [...selection, ...g.ids] : g.ids);
    groups.appendChild(b);
  }
  if (!groups.children.length) {
    groups.innerHTML = '<span class="muted">Nothing else like it</span>';
  }

  const row = $('peer-selected-row');
  row.hidden = sel.length < 2;
  const chips = $('peer-selected');
  chips.innerHTML = '';
  if (sel.length >= 2) {
    for (const s of sel) {
      const b = document.createElement('button');
      b.className = 'chip small';
      b.innerHTML = `${chipLabel(s)}<span class="x">×</span>`;
      b.title = 'Click to un-pick this one';
      b.onclick = () => toggleSelection(s.id);
      chips.appendChild(b);
    }
  }
}

$('btn-clearsel').onclick = () => setSelection([]);
$('btn-done').onclick = () => setSelection([]);

/* --------------------------------------------------------- element list */

function buildList() {
  const list = $('element-list');
  list.innerHTML = '';
  // `html` is built here from escaped/mathHtml'd pieces, never raw user text.
  let col = null;  // the current plot's column
  const add = (html, id, kind, icon, obj = null) => {
    const b = document.createElement('button');
    b.dataset.eid = id;
    const tag = TAGGED_KINDS.has(kind) ? `<span class="kind">${kindLabel(kind, obj)}</span>` : '';
    b.innerHTML = `<span class="ico">${icon}</span><span class="txt">${html}</span>` +
      `${obj && 'text' in obj ? wordsDetail(obj) : ''}${tag}`;
    b.title = 'Click to pick it · Ctrl-click to pick more than one';
    b.onclick = (evt) => {
      if (isMulti(evt)) toggleSelection(id); else setSelection([id]);
    };
    col.appendChild(b);
  };
  const group = (name) => {
    col = document.createElement('div');
    col.className = 'col';
    const d = document.createElement('div');
    d.className = 'group';
    d.textContent = name;
    col.appendChild(d);
    list.appendChild(col);
  };

  if (spec.suptitle?.text) {
    group('Whole figure');
    add(mathHtml(spec.suptitle.text), 'suptitle', 'suptitle', ICON.words, spec.suptitle);
  }

  for (const p of spec.panels) {
    group(capitalize(plotName(p.id)));
    // What people came to change first: the words and the lines...
    if (p.title?.text) add(mathHtml(p.title.text), `${p.id}__title`, 'title', ICON.words, p.title);
    if (p.xlabel?.text) add(mathHtml(p.xlabel.text), `${p.id}__xlabel`, 'xlabel', ICON.words, p.xlabel);
    if (p.ylabel?.text) add(mathHtml(p.ylabel.text), `${p.id}__ylabel`, 'ylabel', ICON.words, p.ylabel);
    for (const k of ['top_axis', 'right_axis']) {
      if (p[k]?.text) add(mathHtml(p[k].text), `${p.id}__${k}`, k, ICON.words, p[k]);
    }
    for (const t of p.texts || []) add(mathHtml(t.text), t.id, 'text', ICON.words, t);
    for (const sr of p.series || []) add(seriesName(sr), sr.id, 'series', seriesIcon(sr.style, sr.kind), sr);
    (p.arrows || []).forEach((ar, i) => add(
      p.arrows.length > 1 ? `Arrow ${i + 1}` : 'Arrow', ar.id, 'arrow', ICON.arrow));
    for (const hv of ['v', 'h']) {
      (p[`${hv}lines`] || []).forEach((l, i) => add(guideName(hv, l), `${p.id}__${hv}line_${i}`,
        'guide', seriesIcon({ color: l.color ?? '0.8', ls: l.ls ?? '-' })));
    }
    if (p.zero_lines) add('Lines through zero', `${p.id}__zero`, 'zero', ICON.zero);
    // ...then the frame around them.
    add('Plot box', `panel:${p.id}`, 'panel', ICON.box);
    add('Bottom numbers', `panel:${p.id}:xticks`, 'xticks', ICON.numbers);
    add('Side numbers', `panel:${p.id}:yticks`, 'yticks', ICON.numbers);
    if (resolve(`${p.id}__legend`)) add('Legend', `${p.id}__legend`, 'legend', ICON.legend);
  }
  markList();
}

/* Kinds whose row shows user text, so it needs a word saying what it is.
 * Rows like "Plot box" already say it. */
const TAGGED_KINDS = new Set(['suptitle', 'title', 'xlabel', 'ylabel', 'top_axis', 'right_axis',
                              'text', 'series', 'guide']);

const ICON = {
  words: '<b class="ico-words">Aa</b>',
  arrow: '<svg viewBox="0 0 22 12"><path d="M3 10 L18 2 M12 2 H18 V7" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  box: '<svg viewBox="0 0 22 12"><rect x="4" y="1" width="14" height="10" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>',
  numbers: '<b class="ico-num">12</b>',
  zero: '<svg viewBox="0 0 22 12"><path d="M1 6 H21 M11 0 V12" stroke="#c4c8cd" stroke-width="1.4"/></svg>',
  legend: '<svg viewBox="0 0 22 12"><path d="M3 3 H9 M3 9 H9" stroke="currentColor" stroke-width="1.6"/><path d="M12 3 H19 M12 9 H19" stroke="currentColor" stroke-width="1" opacity=".5"/></svg>',
};

const DASHES = { '--': '4 2', 'dashed': '4 2', ':': '1 2', 'dotted': '1 2', '-.': '4 2 1 2', 'dashdot': '4 2 1 2' };

/** A tiny picture of a line as it is drawn: its color, dash and marker. */
function seriesIcon(st = {}, kind = 'line') {
  if (kind && kind !== 'line') return layerIcon(st, kind);
  const c = cssColor(st.color);
  const ls = st.ls ?? st.linestyle ?? '-';
  const line = seriesHasLine(st)
    ? `<path d="M1 6 H21" stroke="${c}" stroke-width="1.8" stroke-dasharray="${DASHES[ls] ?? ''}"/>` : '';
  const m = st.marker && st.marker !== 'none' && st.marker !== 'None' ? st.marker : null;
  const mk = {
    '.': `<circle cx="11" cy="6" r="1.8" fill="${c}"/>`,
    'o': `<circle cx="11" cy="6" r="3.2" fill="${c}"/>`,
    's': `<rect x="8" y="3" width="6" height="6" fill="${c}"/>`,
    '^': `<path d="M11 2.5 L14.5 9 H7.5 Z" fill="${c}"/>`,
    'D': `<path d="M11 2 L15 6 L11 10 L7 6 Z" fill="${c}"/>`,
    '+': `<path d="M11 1.5 V10.5 M6.5 6 H15.5" stroke="${c}" stroke-width="1.8"/>`,
    'x': `<path d="M7.5 2.5 L14.5 9.5 M14.5 2.5 L7.5 9.5" stroke="${c}" stroke-width="1.8"/>`,
  }[m] ?? (m ? `<circle cx="11" cy="6" r="2.6" fill="${c}"/>` : '');
  return `<svg viewBox="0 0 22 12">${line}${mk}</svg>`;
}

/** A matplotlib color as CSS. normHex() maps anything it doesn't know to
 *  black, but named colors like "red" are valid CSS as they are. */
function cssColor(c) {
  if (!c) return '#1f77b4';                 // matplotlib's first default color
  const hex = normHex(c);
  if (hex !== '#000000' || /^(k|black|#000(000)?)$/i.test(c)) return hex;
  return escapeHtml(c);
}

/** "● 10 pt": a text element's color and size, at a glance. */
function wordsDetail(o) {
  return `<span class="det"><i class="swatch" style="background:${cssColor(o.color ?? '#000000')}"></i>` +
    `${o.size ?? 12} pt</span>`;
}

/** "Up-down line at x = 0.5": where a guide line sits, in plain words. */
function guideName(hv, l) {
  const at = Number((hv === 'v' ? l.x : l.y).toPrecision(3)).toString().replace('-', '−');
  return hv === 'v' ? `Up-down line at x = ${at}` : `Side-to-side line at y = ${at}`;
}

/** Small pictures for the curve kinds that aren't plain lines. */
function layerIcon(st, kind) {
  const c = cssColor(st.color);
  if (kind === 'band') {
    return `<svg viewBox="0 0 22 12"><path d="M1 4 Q11 0 21 4 V9 Q11 12 1 9 Z" fill="${c}" opacity=".35"/></svg>`;
  }
  if (kind === 'bar') {
    return `<svg viewBox="0 0 22 12"><rect x="2" y="5" width="4" height="7" fill="${c}"/>` +
      `<rect x="9" y="1" width="4" height="11" fill="${c}"/><rect x="16" y="7" width="4" height="5" fill="${c}"/></svg>`;
  }
  if (kind === 'step') {
    return `<svg viewBox="0 0 22 12"><path d="M1 10 H6 V6 H11 V2 H16 V7 H21" stroke="${c}" stroke-width="1.6" fill="none"/></svg>`;
  }
  if (kind === 'heatmap' || kind === 'contourf') {
    return '<svg viewBox="0 0 22 12"><rect x="1" y="1" width="7" height="5" fill="#440154"/>' +
      '<rect x="8" y="1" width="7" height="5" fill="#21918c"/><rect x="15" y="1" width="6" height="5" fill="#fde725"/>' +
      '<rect x="1" y="6" width="7" height="5" fill="#3b528b"/><rect x="8" y="6" width="7" height="5" fill="#5ec962"/>' +
      '<rect x="15" y="6" width="6" height="5" fill="#21918c"/></svg>';
  }
  if (kind === 'contour') {
    return '<svg viewBox="0 0 22 12"><ellipse cx="11" cy="6" rx="9" ry="5" fill="none" stroke="#3b528b" stroke-width="1.2"/>' +
      '<ellipse cx="11" cy="6" rx="5" ry="2.6" fill="none" stroke="#5ec962" stroke-width="1.2"/></svg>';
  }
  if (kind === 'scatter') {
    return '<svg viewBox="0 0 22 12"><circle cx="4" cy="8" r="2" fill="#440154"/><circle cx="10" cy="5" r="3" fill="#21918c"/>' +
      '<circle cx="17" cy="4" r="2.5" fill="#fde725" stroke="#c9b200" stroke-width=".6"/></svg>';
  }
  if (kind === 'box') {
    return `<svg viewBox="0 0 22 12"><path d="M11 0 V3 M11 9 V12" stroke="currentColor" stroke-width="1"/>` +
      `<rect x="6" y="3" width="10" height="6" fill="${c}" fill-opacity=".6" stroke="currentColor" stroke-width=".8"/>` +
      `<path d="M6 6 H16" stroke="currentColor" stroke-width="1.2"/></svg>`;
  }
  return `<svg viewBox="0 0 22 12"><path d="M6 1 V11 M4 1 H8 M4 11 H8 M16 3 V9 M14 3 H18 M14 9 H18" ` +
    `stroke="${c}" stroke-width="1.2" fill="none"/><circle cx="6" cy="6" r="2" fill="${c}"/>` +
    `<circle cx="16" cy="6" r="2" fill="${c}"/></svg>`;
}

/** A line's legend name, or a plain "no name" for helper lines. */
function seriesName(sr) {
  return sr.label ? mathHtml(sr.label) : '<em class="noname">no name</em>';
}

/** How a picked element is named in the "Picked" chips. Returns HTML. */
function chipLabel(s) {
  const where = s.panel ? ` · ${s.panel}` : '';
  if (s.kind === 'panel') return `Plot box${where}`;
  if (s.kind === 'xticks') return `Bottom numbers${where}`;
  if (s.kind === 'yticks') return `Side numbers${where}`;
  if (s.kind === 'legend') return `Legend${where}`;
  if (s.kind === 'series') return seriesName(s.obj);
  if (s.kind === 'arrow') return `Arrow${where}`;
  if (s.kind === 'guide') return guideName(s.axis, s.obj);
  if (s.kind === 'zero') return `Lines through zero${where}`;
  return mathHtml(s.obj.text);
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
  if (!spec) return;  // signed out / still loading: nothing to edit yet
  if (!$('csv-modal').hidden || !$('ai-modal').hidden) return;  // a dialog has the keyboard
  if (evt.key === 'Escape') { setSelection([]); return; }

  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);

  // Ctrl+Z undo; Ctrl+Y or Ctrl+Shift+Z redo.
  const k = evt.key.toLowerCase();
  if ((evt.ctrlKey || evt.metaKey) && (k === 'z' || k === 'y')) {
    // While typing (e.g. retyping a label's text), these should undo/redo
    // keystrokes in that field via the browser's own native text undo,
    // not jump out and move the whole app-level history stack.
    if (typing) return;
    evt.preventDefault();
    if (k === 'y' || evt.shiftKey) redo(); else undo();
    return;
  }

  // Ctrl+A / Ctrl+X / Ctrl+C / Ctrl+V are never intercepted here, so a
  // focused text field always gets the browser's native select-all / cut /
  // copy / paste -- nothing to wire up for those.
  if (typing || !selection.length) return;

  if (evt.key === 'Delete' || evt.key === 'Backspace') {
    evt.preventDefault();
    deleteSelection();
    return;
  }

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
    // Only labels without a box get the default; existing boxes keep theirs.
    if (on) o.bbox ??= { boxstyle: `round,pad=${DEFAULT_BOX_PAD}`, fc: 'white', ec: 'black', lw: 0.8 };
    else delete o.bbox;
  });
  refreshInspector();
});

// No instant preview: a new padding changes the box's size, which only the
// real render knows -- it lands a moment later.
$('f-box-pad').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (!Number.isFinite(v) || v < 0) return;
  edit((o) => { if (o.bbox) o.bbox.boxstyle = withPad(o.bbox.boxstyle, v); });
});

// matplotlib draws the box as a "patch" group inside the label's own group;
// recolor its path now so the change shows before the real render lands.
for (const [fid, key, css] of [['f-box-fc', 'fc', 'fill'], ['f-box-ec', 'ec', 'stroke']]) {
  $(fid).addEventListener('change', (e) => {
    const color = e.target.value;
    edit((o) => { if (o.bbox) o.bbox[key] = color; });
    for (const s of selected()) {
      groupFor(s.id)?.querySelectorAll('g[id^="patch"] path')
        .forEach((p) => { p.style[css] = color; });
    }
    refreshInspector();
  });
}

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
        `Log scale needs every number above zero, but ${plotName(bad.panel)} ` +
        `goes down to zero or below.`;
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

$('f-cmap').addEventListener('change', (e) => edit((o) => {
  o.style ??= {};
  o.style.cmap = e.target.value;
  delete o.style.colors;          // contour lines in one colour -> coloured by level
}));
$('f-cmap-log').addEventListener('change', (e) => edit((o) => {
  o.style ??= {};
  if (e.target.checked) o.style.norm = 'log'; else delete o.style.norm;
}));
for (const [id, key] of [['f-cmap-min', 'vmin'], ['f-cmap-max', 'vmax']]) {
  $(id).addEventListener('change', (e) => {
    const v = e.target.value.trim() === '' ? null : parseFloat(e.target.value);
    if (v !== null && !Number.isFinite(v)) return;
    edit((o) => { o.style ??= {}; if (v === null) delete o.style[key]; else o.style[key] = v; });
  });
}
$('f-cbar').addEventListener('change', (e) => {
  const on = e.target.checked;
  edit((o) => { if (on) o.colorbar = o.colorbar ?? { label: o.label ?? '' }; else delete o.colorbar; });
  $('f-cbar-label-field').hidden = !on;
});
$('f-cbar-label').addEventListener('input', (e) => {
  edit((o) => { if (o.colorbar) o.colorbar.label = e.target.value; }, { immediate: false });
});

$('f-guide-color').addEventListener('input', (e) => {
  $('f-guide-color-hex').value = e.target.value;
  edit((o) => { o.color = e.target.value; }, { immediate: false });
});
$('f-guide-color-hex').addEventListener('change', (e) => {
  const v = e.target.value.trim();
  if (!/^#[0-9a-f]{6}$/i.test(v)) { e.target.value = $('f-guide-color').value; return; }
  $('f-guide-color').value = v;
  edit((o) => { o.color = v; });
});
$('f-guide-ls').addEventListener('change', (e) => edit((o) => { o.ls = e.target.value; }));
$('f-guide-lw').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v >= 0) edit((o) => { o.lw = v; });
});
$('f-guide-pos').addEventListener('change', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v)) edit((o, s) => { o[s.axis === 'v' ? 'x' : 'y'] = v; });
});
$('f-zero').addEventListener('change', (e) => edit((o) => { o.zero_lines = e.target.checked; }));

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

// A snapshot carries the rev it had back then; keep the current one so the
// revision only ever counts up. (The code editor compares revs to tell
// whether the figure has changed since code was saved.)
function undo() {
  if (!history.length) return;
  future.push(clone(spec));
  const rev = spec.rev;
  spec = history.pop();
  spec.rev = rev;
  afterHistoryJump();
}

function redo() {
  if (!future.length) return;
  history.push(clone(spec));
  const rev = spec.rev;
  spec = future.pop();
  spec.rev = rev;
  afterHistoryJump();
}

function afterHistoryJump() {
  lastCoalesce = null;
  refreshUndoButtons();
  scheduleHistorySave();
  buildList();               // a deleted element may be back, or gone again
  setSelection(selection);   // drops ids that no longer exist
  reconcilePreviews();
  scheduleSave(0);
}

$('btn-undo').onclick = undo;
$('btn-redo').onclick = redo;

function deleteSelection() {
  const sel = selected().filter((s) => DELETABLE.has(s.kind));
  if (!sel.length) {
    if (selection.length) setStatus('The plot box and its numbers can’t be deleted', 'err');
    return;
  }
  pushHistory();
  for (const s of sel) {
    const p = s.panel ? panelById(s.panel) : null;
    if (s.kind === 'text') p.texts = p.texts.filter((t) => t.id !== s.id);
    else if (s.kind === 'arrow') p.arrows = p.arrows.filter((a) => a.id !== s.id);
    else if (s.kind === 'series') p.series = p.series.filter((r) => r.id !== s.id);
    else if (s.kind === 'legend') p.legend = null;
    else if (s.kind === 'guide') p[`${s.axis}lines`] = p[`${s.axis}lines`].filter((l) => l !== s.obj);
    else if (s.kind === 'zero') p.zero_lines = false;
    else if (s.kind === 'top_axis' || s.kind === 'right_axis') delete p[s.kind];  // the whole second scale
    else s.obj.text = '';
  }
  buildList();
  setSelection([]);
  reconcilePreviews();
  scheduleSave(0);
}

$('btn-delete').onclick = deleteSelection;

async function download(url, filename) {
  setStatus('Getting it ready…', 'busy');
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
    setStatus(`Downloaded ${filename} ✓`);
  } catch (e) {
    setStatus(e.message, 'err');
  }
}

/* The code editor (another tab) saved code that changed this figure: load
 * the new version. The server put the old spec on the undo stack, so Ctrl+Z
 * here undoes the whole sync. A pending local save is dropped rather than
 * sent -- it would overwrite what the code just did. */
if ('BroadcastChannel' in window) {
  new BroadcastChannel('figforge').onmessage = async (e) => {
    if (e.data?.type !== 'figure-synced' || e.data.project !== FIGURE) return;
    clearTimeout(saveTimer);
    savePending = needsSave = false;
    try {
      const [r, hr] = await Promise.all([fetch(`/api/figure/${FIGURE}`), fetch(`/api/history/${FIGURE}`)]);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.statusText);
      const saved = hr.ok ? await hr.json() : {};
      spec = data.spec;
      renderedSpec = clone(data.spec);
      geometry = data.geometry;
      history = saved.undo || [];
      future = saved.redo || [];
      lastCoalesce = null;
      refreshUndoButtons();
      applySvg(data.svg);
      buildList();
      setSelection(selection);
      setStatus('Updated from your code ✓ (Ctrl+Z to undo)');
    } catch (err) {
      setStatus(`Couldn't use the change from your code: ${err.message}`, 'err');
    }
  };
}

/* figure.py: a small menu -- open the code editor (code.html, a new tab) or
 * download. "Download my edited version" only shows once one is saved. */
$('btn-py').onclick = async () => {
  const menu = $('py-menu');
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.hidden = false;
  $('py-download-edited').hidden = true;
  try {
    const r = await fetch(`/api/script/${FIGURE}`);
    if (r.ok) $('py-download-edited').hidden = !(await r.json()).custom;
  } catch { /* the menu still works without the edited-version entry */ }
};
document.addEventListener('pointerdown', (e) => {
  if (!$('btn-py').parentElement.contains(e.target)) $('py-menu').hidden = true;
  if (!$('btn-help').parentElement.contains(e.target)) showHelp(false);
  if (!$('btn-dl').parentElement.contains(e.target)) $('dl-menu').hidden = true;
});

/* "? Help": the how-to and the keyboard keys, out of the way until asked. */
function showHelp(on) {
  $('help-pop').hidden = !on;
  $('btn-help').setAttribute('aria-expanded', String(on));
}
$('btn-help').onclick = () => showHelp($('help-pop').hidden);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') showHelp(false);
});
$('py-view').onclick = async () => {
  $('py-menu').hidden = true;
  await flushPending();  // the code is generated from what's saved
  window.open(`code.html?project=${encodeURIComponent(FIGURE)}`, '_blank');
};
$('py-download').onclick = () => {
  $('py-menu').hidden = true;
  download(`/api/code/${FIGURE}`, `${FIGURE}.py`);
};
$('py-download-edited').onclick = () => {
  $('py-menu').hidden = true;
  download(`/api/code/${FIGURE}?edited=1`, `${FIGURE}_edited.py`);
};
$('btn-dl').onclick = () => { $('dl-menu').hidden = !$('dl-menu').hidden; };
const exportAs = (fmt) => {
  $('dl-menu').hidden = true;
  download(`/api/export/${FIGURE}?format=${fmt}`, `${FIGURE}.${fmt}`);
};
$('btn-png').onclick = () => exportAs('png');
$('dl-pdf').onclick = () => exportAs('pdf');
$('dl-svg').onclick = () => exportAs('svg');

/* ---------------------------------------------- whole-figure settings */

function refreshFigureFields() {
  if (!spec) return;
  $('f-fig-w').value = spec.size_in[0];
  $('f-fig-h').value = spec.size_in[1];
  $('f-fig-preset').value = '';
  $('f-fig-font').value = spec.rcparams?.['font.family'] ?? 'sans-serif';
}

/** Every text size in the figure, times k (rounded to half points): so a
 *  figure shrunk to one journal column keeps readable, proportionate text. */
function scaleText(k) {
  const r = (v) => Math.max(4, Math.round(v * k * 2) / 2);
  const sz = (o, d) => { if (o) o.size = r(o.size ?? d); };
  sz(spec.suptitle, 16);
  for (const p of spec.panels) {
    sz(p.title, 14); sz(p.xlabel, 13); sz(p.ylabel, 13);
    if (p.top_axis) sz(p.top_axis, 13);
    if (p.right_axis) sz(p.right_axis, 13);
    p.xtick_size = r(p.xtick_size ?? 11);
    p.ytick_size = r(p.ytick_size ?? 11);
    if (p.legend) p.legend.size = r(p.legend.size ?? 10);
    for (const t of p.texts || []) t.size = r(t.size ?? 12);
    for (const c of p.series || []) {
      if (c.colorbar) { c.colorbar.size = r(c.colorbar.size ?? 12); c.colorbar.tick_size = r(c.colorbar.tick_size ?? 10); }
    }
  }
}

function setFigureSize(w, h) {
  if (!(w >= 1 && w <= 40 && h >= 1 && h <= 40)) return;
  pushHistory();
  const k = w / spec.size_in[0];
  if ($('f-fig-scale').checked && Math.abs(k - 1) > 0.01) scaleText(k);
  spec.size_in = [Math.round(w * 100) / 100, Math.round(h * 100) / 100];
  refreshFigureFields();
  scheduleSave(0);
  setTimeout(fit, 0);
}

$('f-fig-preset').addEventListener('change', (e) => {
  const v = e.target.value;
  const [w0, h0] = spec.size_in;
  if (v === 'slide') setFigureSize(10, 5.6);
  else if (v === 'poster') setFigureSize(12, 8);
  else if (v) { const w = parseFloat(v); setFigureSize(w, w * h0 / w0); }   // same proportions
});
$('f-fig-w').addEventListener('change', (e) => setFigureSize(parseFloat(e.target.value), spec.size_in[1]));
$('f-fig-h').addEventListener('change', (e) => {
  // Height alone: the text keeps its size.
  const h = parseFloat(e.target.value);
  if (!(h >= 1 && h <= 40)) return;
  pushHistory();
  spec.size_in = [spec.size_in[0], Math.round(h * 100) / 100];
  scheduleSave(0);
  setTimeout(fit, 0);
});
$('f-fig-font').addEventListener('change', (e) => {
  pushHistory();
  const rc = { ...(spec.rcparams || {}) };
  if (e.target.value === 'sans-serif') delete rc['font.family']; else rc['font.family'] = e.target.value;
  if (e.target.value === 'serif') rc['mathtext.fontset'] = 'dejavuserif'; else delete rc['mathtext.fontset'];
  spec.rcparams = rc;
  scheduleSave(0);
});

/* ------------------------------------------- add a label / an arrow */

/** The plot new things go into: the picked element's, else the first. */
function workingPanel() {
  const s = selected()[0];
  return panelById(s?.panel) || spec.panels[0];
}

function middleOf(p) {
  const mid = (lim, scale) => scale === 'log' ? Math.sqrt(lim[0] * lim[1]) : (lim[0] + lim[1]) / 2;
  const at = (lim, scale, f) => scale === 'log'
    ? lim[0] * Math.pow(lim[1] / lim[0], f) : lim[0] + (lim[1] - lim[0]) * f;
  return { x: mid(p.xlim, p.xscale), y: mid(p.ylim, p.yscale),
           xAt: (f) => at(p.xlim, p.xscale, f), yAt: (f) => at(p.ylim, p.yscale, f) };
}

function freshId(p, word) {
  const used = new Set(spec.panels.flatMap((q) => ['texts', 'series', 'arrows']
    .flatMap((k) => (q[k] || []).map((i) => i.id))));
  let n = 1;
  while (used.has(`${p.id}_${word}${n}`)) n++;
  return `${p.id}_${word}${n}`;
}

$('btn-add-label').onclick = () => {
  const p = workingPanel();
  const m = middleOf(p);
  const id = freshId(p, 'label');
  pushHistory();
  (p.texts ??= []).push({ id, text: 'New label', xy: [m.x, m.y], coords: 'data',
                          size: 12, color: '#000000', ha: 'center', va: 'center' });
  buildList();
  setSelection([id]);
  scheduleSave(0);
  setTimeout(() => { $('f-text').focus(); $('f-text').select(); }, 0);
};

$('btn-add-arrow').onclick = () => {
  const p = workingPanel();
  const m = middleOf(p);
  const id = freshId(p, 'arrow');
  pushHistory();
  (p.arrows ??= []).push({ id, p0: [m.xAt(0.35), m.yAt(0.5)], p1: [m.xAt(0.65), m.yAt(0.5)],
                           arrowstyle: '->', color: '#000000', lw: 1.5, mutation_scale: 14, zorder: 6 });
  buildList();
  setSelection([id]);
  scheduleSave(0);
  setStatus('Arrow added: drag the middle to move it, an end to stretch it');
};

$('btn-delete-project').onclick = async () => {
  if (!confirm(`Delete "${FIGURE}" and everything in it? This can't be undone.`)) return;
  await flushPending();
  try {
    const r = await fetch('/api/project/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: FIGURE }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    try { localStorage.removeItem(LAST_PROJECT_KEY); } catch { /* fine */ }
    location.search = '?project=' + encodeURIComponent(d.figures[0]);
  } catch (e) { setStatus(e.message, 'err'); }
};

// Two-step confirm rather than a modal dialog.
let rebuildArmed = false;
$('btn-rebuild').onclick = async () => {
  const btn = $('btn-rebuild');
  if (!rebuildArmed) {
    rebuildArmed = true;
    btn.textContent = 'Sure? Click again';
    setTimeout(() => {
      if (rebuildArmed) { rebuildArmed = false; btn.textContent = 'Start over'; }
    }, 4000);
    return;
  }
  rebuildArmed = false;
  btn.textContent = 'Start over';
  setStatus('Starting over…', 'busy');
  try {
    const r = await fetch(`/api/rebuild/${FIGURE}`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    history = [];  // the server already cleared history.json
    future = [];
    refreshUndoButtons();
    spec = data.spec;
    renderedSpec = clone(data.spec);
    geometry = data.geometry;
    setSelection([]);
    applySvg(data.svg);
    buildList();
    setStatus('Started over from your data ✓');
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
  const h = $('canvas-wrap').clientHeight - 40;
  const scale = Math.min(w / geometry.width, h / geometry.height);
  zoom = Math.max(40, Math.min(220, Math.round(scale * 100)));
  $('zoom').value = zoom;
  $('zoomval').textContent = zoom + '%';
  applyZoom();
}

/* The bottom bar's height: drag its top edge. Remembered per browser. */
const DOCK_KEY = 'figforge.dockHeight';
function setDockHeight(px) {
  const max = $('stage').clientHeight - 160;   // always leave room for the figure
  $('dock').style.height = Math.max(90, Math.min(max, px)) + 'px';
}
try {
  const saved = Number(localStorage.getItem(DOCK_KEY));
  if (saved) setDockHeight(saved);
} catch { /* storage blocked: keep the default height */ }

$('dock-handle').addEventListener('pointerdown', (e) => {
  e.preventDefault();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);
  const startY = e.clientY, startH = $('dock').offsetHeight;
  const move = (ev) => setDockHeight(startH + startY - ev.clientY);
  const up = () => {
    handle.removeEventListener('pointermove', move);
    handle.removeEventListener('pointerup', up);
    try { localStorage.setItem(DOCK_KEY, String($('dock').offsetHeight)); } catch { /* fine */ }
    if (geometry) fit();
  };
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up);
});

/* ----------------------------------------------------------- projects */

function readLastProject() {
  try { return localStorage.getItem(LAST_PROJECT_KEY); } catch { return null; }
}

function rememberProject(name) {
  try { localStorage.setItem(LAST_PROJECT_KEY, name); } catch { /* private window */ }
}

function fillProjectSelect(names) {
  const sel = $('project-select');
  sel.innerHTML = names
    .map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`)
    .join('');
  sel.value = FIGURE;
}

/** Switching is a full reload onto ?project=<name>: every piece of editor
 *  state (selection, previews, caches, undo) starts clean for the new one. */
async function openProject(name) {
  setStatus('Saving…', 'busy');
  await flushPending();
  rememberProject(name);
  location.search = '?project=' + encodeURIComponent(name);
}

async function projectOp(op, name) {
  await flushPending();
  const r = await fetch(`/api/project/${op}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ from: FIGURE, name }),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data.name;
}

function askName(message, initial) {
  const name = (prompt(message, initial) || '').trim();
  if (!name) return null;
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    setStatus('Names can only use letters, numbers, - and _', 'err');
    return null;
  }
  return name;
}

$('project-select').onchange = (e) => {
  if (e.target.value !== FIGURE) openProject(e.target.value);
};

/* ------------------------------------------------ new from a CSV file
 * The server reads the file (so the browser and figure.py agree on how it
 * parses), lists its columns, and builds the figure from the picks. */

let csvText = null, csvFilename = '';
let assistantOn = false;   // /api/auth/me: the AI assistant is set up and you may use it

function csvMessage(msg, cls = '') {
  $('csv-msg').textContent = msg;
  $('csv-msg').className = 'csv-msg ' + cls;
}

function openCsvDialog() {
  csvText = null;
  $('csv-file').value = '';
  $('csv-pick').hidden = true;
  $('csv-create').disabled = true;
  $('csv-drop-text').innerHTML = '<b>Choose a CSV file</b> or drop it here<br>' +
    '<em>comma, semicolon, tab or space separated · up to 4 MB</em>';
  csvMessage('');

  $('csv-modal').hidden = false;
}

function closeCsvDialog() { $('csv-modal').hidden = true; }

function projectNameFrom(filename) {
  let base = filename.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '_')
    .replace(/^_+|_+$/g, '').slice(0, 40) || 'figure';
  const taken = new Set([...$('project-select').options].map((o) => o.value));
  let name = base, n = 2;
  while (taken.has(name)) name = `${base}_${n++}`;
  return name;
}

const fmt = (v) => (Math.abs(v) >= 1e5 || (Math.abs(v) < 1e-3 && v !== 0)) ? v.toExponential(2) : +v.toPrecision(4);

async function loadCsvFile(file) {
  if (!file) return;
  if (file.size > 4 * 1024 * 1024) { csvMessage('That file is over 4 MB.', 'err'); return; }
  csvMessage('Reading…');
  csvText = await file.text();
  csvFilename = file.name;
  try {
    const r = await fetch('/api/csv/inspect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ csv: csvText }),
    });
    const info = await r.json();
    if (!r.ok) throw new Error(info.error || r.statusText);
    const numeric = info.columns.filter((c) => c.numeric);
    $('csv-drop-text').innerHTML = `<b>${escapeHtml(file.name)}</b> · choose another file`;
    $('csv-summary').textContent = `${info.rows} rows · ${info.columns.length} columns · ${info.separator}-separated. ` +
      'Pick the x column and one or more columns to plot.';
    $('csv-cols').innerHTML = info.columns.map((c) => {
      const range = c.numeric ? `${fmt(c.min)} … ${fmt(c.max)}` : 'not numbers';
      const dis = c.numeric ? '' : 'disabled';
      const isX = c.index === numeric[0]?.index;
      const isY = numeric.length > 1 && c.index === numeric[1].index;
      return `<tr class="${c.numeric ? '' : 'muted'}"><td>${escapeHtml(c.name)}</td><td>${range}</td>` +
        `<td><input type="radio" name="csv-x" value="${c.index}" ${dis} ${isX ? 'checked' : ''}></td>` +
        `<td><input type="checkbox" name="csv-y" value="${c.index}" ${dis} ${isY ? 'checked' : ''}></td></tr>`;
    }).join('');
    $('csv-name').value = projectNameFrom(file.name);
    $('csv-pick').hidden = false;
    csvMessage(numeric.length < 2 ? 'Only one column holds numbers, so there is nothing to plot against it.' : '',
               numeric.length < 2 ? 'err' : '');
    refreshCsvCreate();
  } catch (e) {
    csvText = null;
    $('csv-pick').hidden = true;
    csvMessage(`Couldn’t read that file: ${e.message}`, 'err');
  }
}

function csvChoice() {
  const x = document.querySelector('input[name="csv-x"]:checked');
  const ys = [...document.querySelectorAll('input[name="csv-y"]:checked')].map((i) => +i.value);
  return { x: x ? +x.value : null, ys: ys.filter((y) => y !== (x ? +x.value : null)) };
}

function refreshCsvCreate() {
  const { x, ys } = csvChoice();
  $('csv-create').disabled = !(csvText && x !== null && ys.length && /^[A-Za-z0-9_-]+$/.test($('csv-name').value));
}

$('btn-new').onclick = openCsvDialog;
$('csv-close').onclick = closeCsvDialog;
$('csv-cancel').onclick = closeCsvDialog;
$('csv-file').onchange = (e) => loadCsvFile(e.target.files[0]);
$('csv-cols').addEventListener('change', (e) => {
  // The x column can't also be a y column.
  if (e.target.name === 'csv-x') {
    const y = document.querySelector(`input[name="csv-y"][value="${e.target.value}"]`);
    if (y) y.checked = false;
  }
  refreshCsvCreate();
});
$('csv-name').addEventListener('input', refreshCsvCreate);
// At the document level: after picking a file, focus isn't inside the dialog.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('csv-modal').hidden) closeCsvDialog();
});
const drop = $('csv-drop');
['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => loadCsvFile(e.dataTransfer.files[0]));

async function createFromCsv() {
  const { x, ys } = csvChoice();
  const name = $('csv-name').value.trim();
  $('csv-create').disabled = true;
  csvMessage('Building the figure…');
  try {
    const r = await fetch('/api/project/from-csv', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, csv: csvText, filename: csvFilename, x, ys, plot: $('csv-plot').value }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    openProject(d.name);
  } catch (e) {
    csvMessage(e.message, 'err');
    refreshCsvCreate();
  }
}
$('csv-create').onclick = createFromCsv;

/* ------------------------------------------------- ✨ New with AI
 * Any files go to Claude's sandbox on Anthropic's side. Zips are opened
 * here, in the browser, so installers, slides and duplicate copies can be
 * left out and the upload fits the server's 4.5 MB limit. */

const AI_BUDGET = 3.8 * 1024 * 1024;   // bytes of file data per upload
const AI_SKIP = [
  [/\.(exe|msi|dll|bin|iso|dmg|app|so|dylib)$/i, 'program or installer'],
  [/\.(pptx?|key|odp)$/i, 'slides'],
  [/\.(docx?|odt|pages|rtf)$/i, 'document (not read yet)'],
  [/\.(mp4|mov|avi|mkv|mp3|wav)$/i, 'video or audio'],
  [/\.(zip|7z|rar|tar|gz|tgz)$/i, 'archive inside an archive'],
  [/(^|\/)(__MACOSX\/|\.DS_Store$|Thumbs\.db$|desktop\.ini$)/i, 'system file'],
  [/\.bak$/i, 'backup copy'],
];
const AI_BINARY_OK = /\.(xlsx|xls|png|jpe?g|gif|webp|npz|npy|h5|hdf5|mat|pdf)$/i;

let aiFiles = [];        // [{name, bytes: Uint8Array}] to upload
let aiSkipped = [];      // [{name, size, reason}]
let aiRunning = false;

/** Minimal .zip reader: stored and deflated entries, via the browser's own
 *  DecompressionStream. Enough for zips made by Windows, macOS and Python. */
async function unzip(buf) {
  const dv = new DataView(buf);
  let eocd = -1;
  for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 65557); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('that .zip file looks damaged');
  const count = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const out = [];
  const dec = new TextDecoder();
  for (let n = 0; n < count; n++) {
    if (dv.getUint32(p, true) !== 0x02014b50) throw new Error('that .zip file looks damaged');
    const method = dv.getUint16(p + 10, true);
    const csize = dv.getUint32(p + 20, true), usize = dv.getUint32(p + 24, true);
    const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
    const local = dv.getUint32(p + 42, true);
    const name = dec.decode(new Uint8Array(buf, p + 46, nlen));
    p += 46 + nlen + xlen + clen;
    if (name.endsWith('/')) continue;                       // a folder
    const start = local + 30 + dv.getUint16(local + 26, true) + dv.getUint16(local + 28, true);
    out.push({ name, size: usize, method, raw: new Uint8Array(buf, start, csize) });
  }
  return out;
}

async function inflate(entry) {
  if (entry.method === 0) return entry.raw;
  if (entry.method !== 8) throw new Error(`${entry.name}: unsupported zip compression`);
  const stream = new Blob([entry.raw]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function sha(bytes) {
  const h = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return [...h.slice(0, 12)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function looksBinary(bytes) {
  const n = Math.min(bytes.length, 4096);
  for (let i = 0; i < n; i++) if (bytes[i] === 0) return true;
  return false;
}

async function addAiFiles(list) {
  $('ai-msg').textContent = 'Reading…';
  const candidates = [];   // {name, size, get: () => Promise<Uint8Array>}
  for (const file of list) {
    if (/\.zip$/i.test(file.name)) {
      try {
        for (const e of await unzip(await file.arrayBuffer())) {
          candidates.push({ name: e.name, size: e.size, get: () => inflate(e) });
        }
      } catch (err) {
        aiMessage(`${file.name}: ${err.message}`, 'err');
        return;
      }
    } else {
      candidates.push({ name: file.name, size: file.size,
                        get: async () => new Uint8Array(await file.arrayBuffer()) });
    }
  }
  const seen = new Map(aiFiles.map((f) => [f.hash, f.name]));
  let used = aiFiles.reduce((t, f) => t + f.cost, 0);
  // Small files first, so one huge file can't crowd out the real data.
  candidates.sort((a, b) => a.size - b.size);
  for (const c of candidates) {
    const rule = AI_SKIP.find(([re]) => re.test(c.name));
    if (rule) { aiSkipped.push({ name: c.name, size: c.size, reason: rule[1] }); continue; }
    const bytes = await c.get();
    const binary = looksBinary(bytes);
    if (binary && !AI_BINARY_OK.test(c.name)) {
      aiSkipped.push({ name: c.name, size: c.size, reason: 'unknown binary file' });
      continue;
    }
    const hash = await sha(bytes);
    if (seen.has(hash)) {
      aiSkipped.push({ name: c.name, size: c.size, reason: `same as ${seen.get(hash)}` });
      continue;
    }
    const cost = binary ? Math.ceil(bytes.length * 4 / 3) : bytes.length;
    if (used + cost > AI_BUDGET) {
      aiSkipped.push({ name: c.name, size: c.size, reason: 'too much for one upload' });
      continue;
    }
    seen.set(hash, c.name);
    used += cost;
    aiFiles.push({ name: c.name, bytes, binary, hash, cost });
  }
  showAiFiles();
  if (!$('ai-name').value && aiFiles.length) {
    const first = list[0]?.name || aiFiles[0].name;
    $('ai-name').value = projectNameFrom(first.split('/').pop());
  }
  aiMessage('');
  refreshAiMake();
}

function showAiFiles() {
  const size = aiFiles.reduce((t, f) => t + f.bytes.length, 0);
  const kb = (n) => n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;
  $('ai-picked').hidden = !aiFiles.length && !aiSkipped.length;
  $('ai-drop-text').innerHTML = aiFiles.length
    ? `<b>${aiFiles.length} file${aiFiles.length === 1 ? '' : 's'} ready</b> · add more`
    : '<b>Choose your data files</b> or drop them here<br><em>any files or a .zip · a picture of a figure you like helps too</em>';
  $('ai-files-summary').textContent = `Sending ${aiFiles.length} file${aiFiles.length === 1 ? '' : 's'} ` +
    `(${kb(size)}) to Claude.` + (aiSkipped.length ? ` Left out ${aiSkipped.length}.` : '');
  $('ai-file-list').innerHTML =
    aiFiles.map((f) => `<li>${escapeHtml(f.name)} · ${kb(f.bytes.length)}</li>`).join('') +
    aiSkipped.map((f) => `<li class="skip">${escapeHtml(f.name)} — left out: ${escapeHtml(f.reason)}</li>`).join('');
}

function aiMessage(msg, cls = '') {
  $('ai-msg').textContent = msg;
  $('ai-msg').className = 'csv-msg ' + cls;
}

function refreshAiMake() {
  if ($('ai-make').dataset.open) return;
  $('ai-make').disabled = aiRunning || !(aiFiles.length && $('ai-idea').value.trim()
    && /^[A-Za-z0-9_-]+$/.test($('ai-name').value));
}

function openAiDialog() {
  aiFiles = []; aiSkipped = [];
  $('ai-files').value = '';
  $('ai-idea').value = '';
  $('ai-name').value = '';
  $('ai-run').hidden = true;
  delete $('ai-make').dataset.open;
  $('ai-make').textContent = '✨ Make it';
  showAiFiles();
  $('ai-picked').hidden = true;
  aiMessage('');
  refreshAiMake();
  $('ai-modal').hidden = false;
}

function closeAiDialog() { $('ai-modal').hidden = true; }

function bytesToBase64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function runAi() {
  const name = $('ai-name').value.trim();
  const log = $('ai-log');
  const line = (text, cls = '') => {
    log.querySelector('li.now')?.remove();
    const li = document.createElement('li');
    li.textContent = text;
    li.className = cls;
    log.appendChild(li);
  };
  const post = async (url, body) => {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify(body ?? {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(d.error || r.statusText), { status: r.status });
    return d;
  };
  aiRunning = true;
  refreshAiMake();
  log.innerHTML = '';
  $('ai-reply').hidden = true;
  $('ai-run').hidden = false;
  aiMessage('');
  try {
    line('Sending your files…', 'now');
    const dec = new TextDecoder('utf-8', { fatal: true });
    const files = aiFiles.map((f) => {
      if (!f.binary) {
        try { return { name: f.name, text: dec.decode(f.bytes) }; } catch { /* not UTF-8: send as bytes */ }
      }
      return { name: f.name, b64: bytesToBase64(f.bytes) };
    });
    await post('/api/assistant/start', { name, idea: $('ai-idea').value, files, skipped: aiSkipped });
    line('Files sent');
    let d = null, retries = 0;
    while (!d?.done) {
      line('Claude is working…', 'now');
      try {
        d = await post(`/api/assistant/step/${encodeURIComponent(name)}`);
      } catch (e) {
        // A slow or busy step saved nothing, so it can simply run again.
        if ([503, 504].includes(e.status) && retries++ < 2) continue;
        throw e;
      }
      for (const ev of d.events) line(ev);
    }
    log.querySelector('li.now')?.remove();
    const reply = $('ai-reply');
    reply.textContent = d.reply;
    const cost = document.createElement('small');
    cost.textContent = `${d.steps} step${d.steps === 1 ? '' : 's'} · about $${d.cost_usd.toFixed(2)}`;
    reply.appendChild(cost);
    reply.hidden = false;
    if (d.created) {
      $('ai-make').dataset.open = '1';
      $('ai-make').textContent = 'Open the figure';
      $('ai-make').disabled = false;
    } else {
      aiMessage('No figure was made.', 'err');
    }
  } catch (e) {
    log.querySelector('li.now')?.remove();
    aiMessage(`The AI couldn't finish: ${e.message}`, 'err');
  } finally {
    aiRunning = false;
    refreshAiMake();
  }
}

$('btn-ai').onclick = openAiDialog;
$('ai-close').onclick = closeAiDialog;
$('ai-cancel').onclick = closeAiDialog;
$('ai-files').onchange = (e) => addAiFiles([...e.target.files]);
$('ai-idea').addEventListener('input', refreshAiMake);
$('ai-name').addEventListener('input', refreshAiMake);
$('ai-make').onclick = () => ($('ai-make').dataset.open ? openProject($('ai-name').value.trim()) : runAi());
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('ai-modal').hidden && !aiRunning) closeAiDialog();
});
const aiDrop = $('ai-drop');
['dragenter', 'dragover'].forEach((t) => aiDrop.addEventListener(t, (e) => { e.preventDefault(); aiDrop.classList.add('over'); }));
['dragleave', 'drop'].forEach((t) => aiDrop.addEventListener(t, (e) => { e.preventDefault(); aiDrop.classList.remove('over'); }));
aiDrop.addEventListener('drop', (e) => addAiFiles([...e.dataTransfer.files]));

$('btn-saveas').onclick = async () => {
  const name = askName('Save a copy of this project as:', `${FIGURE}-copy`);
  if (!name) return;
  try {
    openProject(await projectOp('duplicate', name));
  } catch (e) { setStatus(e.message, 'err'); }
};

$('btn-rename').onclick = async () => {
  const name = askName('Rename this project to:', FIGURE);
  if (!name || name === FIGURE) return;
  try {
    openProject(await projectOp('rename', name));
  } catch (e) { setStatus(e.message, 'err'); }
};

window.addEventListener('beforeunload', (e) => {
  if (savePending || inFlight || needsSave || historyPending) {
    saveHistoryNow();
    e.preventDefault();
    e.returnValue = '';
  }
});

/* --------------------------------------------------------------- boot */

async function pickProject() {
  const r = await fetch('/api/figures');
  const listing = await r.json();
  const names = listing.figures || [];
  // Only worth a badge when running on this computer; online is the norm.
  $('storage-badge').hidden = (listing.storage || 'local') !== 'local';
  if (!names.length) throw new Error('no projects found in figures/');
  const wanted = [new URLSearchParams(location.search).get('project'), readLastProject()];
  const name = wanted.find((n) => n && names.includes(n)) || names[0];
  return { name, names };
}

/* ----------------------------------------------------------- accounts
 *
 * Hosted only. The server keeps the session in HttpOnly cookies, so all
 * this page ever does is ask /api/auth/me who's signed in and post forms.
 * Email links (confirm sign-up, reset password) land here with Supabase's
 * tokens in the URL fragment; they're handed to /api/auth/session and
 * scrubbed from the address bar right away.
 */

async function authPost(op, body = {}) {
  const r = await fetch(`/api/auth/${op}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function authMessage(msg, cls = '') {
  const el = $('auth-msg');
  el.textContent = msg;
  el.className = 'auth-msg ' + cls;
}

let inviteOnly = true;

function showAuth(view = 'login', msg = '', cls = '') {
  $('auth-screen').hidden = false;
  // Invite-only: no Sign up tab, just a note on the log-in screen.
  document.querySelector('#auth-tabs [data-view="signup"]').hidden = inviteOnly;
  $('invite-note').hidden = !(inviteOnly && view === 'login');
  document.querySelectorAll('.auth-form').forEach((f) => { f.hidden = f.dataset.view !== view; });
  $('auth-tabs').hidden = !['login', 'signup'].includes(view) || inviteOnly;
  document.querySelectorAll('#auth-tabs button').forEach((b) => {
    b.classList.toggle('on', b.dataset.view === view);
  });
  authMessage(msg, cls);
  document.querySelector(`.auth-form[data-view="${view}"] input`)?.focus();
}

document.querySelectorAll('#auth-tabs button, .auth-form [data-goto]').forEach((b) => {
  b.onclick = () => showAuth(b.dataset.view || b.dataset.goto);
});

function busy(form, on) {
  form.querySelectorAll('button, input').forEach((el) => { el.disabled = on; });
}

document.querySelectorAll('.auth-form').forEach((form) => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(form));
    const view = form.dataset.view;
    busy(form, true);
    authMessage('…');
    try {
      if (view === 'login') {
        await authPost('login', f);
        location.reload();
      } else if (view === 'signup') {
        const res = await authPost('signup', f);
        if (res.confirm) {
          form.reset();
          showAuth('login', `Almost done: we sent a confirmation link to ${f.email}. ` +
                            'Open it to activate your account.', 'ok');
        } else {
          location.reload();
        }
      } else if (view === 'forgot') {
        await authPost('recover', f);
        showAuth('login', `If ${f.email} has an account, a reset link is on its way.`, 'ok');
      } else if (view === 'welcome') {
        await authPost('password', { password: f.password });
        await authPost('profile', { display_name: f.display_name });
        location.replace(location.pathname);
      } else if (view === 'reset') {
        if (f.password !== f.password2) throw new Error('the two passwords don’t match');
        await authPost('password', { password: f.password });
        location.replace(location.pathname);
      }
    } catch (err) {
      authMessage(err.message, 'err');
    } finally {
      busy(form, false);
    }
  });
});

/** Tokens or an error that an email link put in the fragment, if any. */
function takeAuthFragment() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (!h.has('access_token') && !h.has('error_description')) return null;
  window.history.replaceState(null, '', location.pathname + location.search);
  return Object.fromEntries(h);
}

async function loadInvites() {
  try {
    const r = await fetch('/api/admin/users');
    if (!r.ok) return;
    const { users } = await r.json();
    $('invite-list').innerHTML = users.map((u) =>
      `<li><span class="who" title="${escapeHtml(u.email)}">${escapeHtml(u.display_name || u.email)}</span>` +
      `<span class="state ${u.status}">${u.admin ? 'admin' : u.status}` +
      (u.status === 'invited'
        ? `<button type="button" class="relink" data-email="${escapeHtml(u.email)}" title="A fresh invite link to send them yourself">Copy link</button>`
        : '') + '</span></li>').join('');
  } catch { /* the list is a convenience */ }
}

/** Make an invite link (no email) and put it on the clipboard. */
async function copyInviteLink(email) {
  try {
    const r = await fetch('/api/admin/invite-link', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    const out = $('invite-link-out');
    out.value = d.link;
    out.hidden = false;
    let copied = true;
    try { await navigator.clipboard.writeText(d.link); } catch { copied = false; out.select(); }
    profileMessage(`${copied ? 'Link copied' : 'Link ready — copy it from the box'} for ${d.email}. ` +
                   'Send it to them yourself; it works once, so send it soon.', 'ok');
    loadInvites();
  } catch (err) { profileMessage(err.message, 'err'); }
}

$('invite-link').onclick = () => {
  const email = $('invite-email').value.trim();
  if (!email) { profileMessage('Type their email first.', 'err'); return; }
  copyInviteLink(email).then(() => { $('invite-email').value = ''; });
};
$('invite-list').addEventListener('click', (e) => {
  const b = e.target.closest('.relink');
  if (b) copyInviteLink(b.dataset.email);
});

$('invite-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = $('invite-email').value.trim();
  if (!email) return;
  try {
    const r = await fetch('/api/admin/invite', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    $('invite-email').value = '';
    profileMessage(`Invite sent to ${d.email}.`, 'ok');
    loadInvites();
  } catch (err) { profileMessage(err.message, 'err'); }
});

function showAccount(user) {
  const name = user.display_name || user.email;
  $('account').hidden = false;
  $('account-name').textContent = name;
  $('account-avatar').textContent = (name.trim()[0] || '?').toUpperCase();
  $('profile-email').textContent = `Signed in as ${user.email}`;
  $('profile-username').value = user.email;
  $('profile-name').value = user.display_name || '';
  $('admin-box').hidden = !user.admin;
  if (user.admin) loadInvites();
}

function profileMessage(msg, cls = '') {
  $('profile-msg').textContent = msg;
  $('profile-msg').className = 'profile-msg ' + cls;
}

$('btn-account').onclick = () => {
  $('profile-panel').hidden = !$('profile-panel').hidden;
  profileMessage('');
};
document.addEventListener('pointerdown', (e) => {
  if (!$('account').contains(e.target)) $('profile-panel').hidden = true;
});

$('btn-profile-save').onclick = async () => {
  try {
    const { user } = await authPost('profile', { display_name: $('profile-name').value });
    showAccount(user);
    profileMessage('Name saved.', 'ok');
  } catch (err) { profileMessage(err.message, 'err'); }
};

$('profile-password-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pw = $('profile-pw').value;
  try {
    if (pw !== $('profile-pw2').value) throw new Error('the two passwords don’t match');
    await authPost('password', { password: pw });
    e.target.reset();
    profileMessage('Password updated.', 'ok');
  } catch (err) { profileMessage(err.message, 'err'); }
});

$('btn-signout').onclick = async () => {
  await flushPending();
  await authPost('logout').catch(() => {});
  location.reload();
};

// A session that ends mid-edit (expired, signed out elsewhere) turns every
// API call into a 401: send the person to log in instead of failing quietly.
const rawFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const r = await rawFetch(...args);
  const url = String(args[0]?.url ?? args[0]);
  if (r.status === 401 && url.startsWith('/api/') && !url.startsWith('/api/auth/')) {
    showAuth('login', 'Your session ended. Please log in again.', 'err');
  }
  return r;
};

/** Who's here, before anything else loads: local mode needs no account;
 *  hosted mode shows the editor only to a signed-in user. */
async function boot() {
  const link = takeAuthFragment();
  if (link?.error_description) {
    showAuth('login', link.error_description.replace(/\+/g, ' '), 'err');
    return;
  }
  if (link?.access_token) {
    try {
      await authPost('session', { access_token: link.access_token,
                                  refresh_token: link.refresh_token });
    } catch (err) {
      showAuth('login', `That link didn’t work: ${err.message}`, 'err');
      return;
    }
  }
  const r = await fetch('/api/auth/me');
  const me = await r.json().catch(() => ({ mode: 'local' }));
  inviteOnly = me.invite_only !== false;
  assistantOn = !!me.assistant;
  $('btn-ai').hidden = !assistantOn;
  if (link?.type === 'recovery') { showAuth('reset'); return; }
  if (link?.type === 'invite') { showAuth('welcome'); return; }
  if (me.mode === 'online') {
    if (!me.user) { showAuth('login'); return; }
    showAccount(me.user);
  }
  startEditor();
}

// An email link opened in a tab that already shows FigForge only changes the
// fragment -- no page load, so boot() wouldn't see it. Reload to run it.
window.addEventListener('hashchange', () => {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.has('access_token') || h.has('error_description')) location.reload();
});

boot();

async function startEditor() {
  setStatus('Opening…', 'busy');
  try {
    const picked = await pickProject();
    FIGURE = picked.name;
    rememberProject(FIGURE);
    fillProjectSelect(picked.names);
    document.title = `FigForge — ${FIGURE}`;
    // `history` in this file is the undo stack; the browser's is window.history.
    window.history.replaceState(null, '', '?project=' + encodeURIComponent(FIGURE));

    const [r, hr] = await Promise.all([
      fetch(`/api/figure/${FIGURE}`),
      fetch(`/api/history/${FIGURE}`),
    ]);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    spec = data.spec;
    renderedSpec = clone(data.spec);
    geometry = data.geometry;
    const saved = hr.ok ? await hr.json() : {};
    history = saved.undo || [];
    future = saved.redo || [];
    refreshUndoButtons();
    applySvg(data.svg);
    buildList();
    refreshFigureFields();
    fit();
    setStatus('');
    $('status').dataset.ready = '1';  // "the figure is on screen" (tests wait on it)
  } catch (e) {
    setStatus(e.message, 'err');
    canvas.innerHTML = `<div style="padding:40px;font:14px sans-serif;color:#b4392b">
      Couldn't open this figure: ${escapeHtml(e.message)}<br><br>
      Try reloading the page.</div>`;
  }
}
