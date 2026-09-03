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
  for (const p of source.panels) {
    for (const t of p.texts || []) {
      if (t.id === id) return { id, kind: 'text', obj: t, draggable: true, panel: p.id };
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
    for (const t of p.texts || []) out.push(resolve(t.id, source));
  }
  return out.filter(Boolean);
}

const KIND_LABEL = {
  suptitle: 'figure title', title: 'panel title', panel: 'panel axes',
  xlabel: 'x-axis label', ylabel: 'y-axis label', text: 'label',
  xticks: 'x-axis ticks', yticks: 'y-axis ticks',
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

/** SVG user units -> data coords for a panel (spec section 3.3). */
function svgToData(panelId, x, y) {
  const g = geometry.panels[panelId];
  const [bx, by, bw, bh] = g.bbox;
  const fx = (x - bx) / bw;
  const fy = (by + bh - y) / bh;   // SVG y runs down, data y runs up
  return [invAxis(g.xlim, g.xscale, fx), invAxis(g.ylim, g.yscale, fy)];
}

/** data coords -> SVG user units, the inverse of svgToData. */
function dataToSvg(panelId, x, y) {
  const g = geometry.panels[panelId];
  const [bx, by, bw, bh] = g.bbox;
  return [bx + axisFrac(g.xlim, g.xscale, x) * bw,
          by + bh - axisFrac(g.ylim, g.yscale, y) * bh];
}

/** Round to ~1/10000 of the axis range so spec.json stays readable. */
function roundTo(v, lim) {
  const range = Math.abs(lim[1] - lim[0]) || 1;
  const d = Math.min(12, Math.max(0, Math.ceil(-Math.log10(range / 1e4))));
  return Number(v.toFixed(d));
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
    addHitTarget(g);
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
  svgEl?.querySelectorAll('.ff-outline').forEach((n) => n.remove());
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

  if (!selection.length) return;
  if (drag) {
    // Mid-drag, recomputing getBBox every frame forces a synchronous layout.
    // The outline shapes haven't changed, so just carry the same transforms.
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
  if (!sel.length) return;

  const multi = sel.length > 1;
  const kinds = [...new Set(sel.map((s) => s.kind))];
  $('insp-kind').textContent = kinds.length === 1
    ? KIND_LABEL[kinds[0]] : 'mixed';
  $('insp-id').textContent = multi ? `${sel.length} selected` : sel[0].id;

  const allPanels = sel.every((s) => s.kind === 'panel'
    || s.kind === 'xticks' || s.kind === 'yticks');
  $('label-fields').hidden = allPanels;
  $('axes-fields').hidden = !allPanels;
  if (allPanels) { refreshAxesInspector(); return; }

  // Retyping many labels at once is meaningless; offer it only for one.
  $('text-field').hidden = multi;
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
  }

  group('figure');
  if (spec.suptitle?.text) add(preview(spec.suptitle.text), 'suptitle', 'sup');

  for (const p of spec.panels) {
    group(`panel (${p.id})`);
    if (p.title?.text) add(preview(p.title.text), `${p.id}__title`, 'ttl');
    if (p.xlabel?.text) add(preview(p.xlabel.text), `${p.id}__xlabel`, 'x');
    if (p.ylabel?.text) add(preview(p.ylabel.text), `${p.id}__ylabel`, 'y');
    for (const t of p.texts || []) add(preview(t.text), t.id, '¶');
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

canvas.addEventListener('pointerdown', (evt) => {
  const g = evt.target.closest('g[id^="t_"]');
  if (g) {
    // A click on one tick label selects every tick on that axis as a
    // single group; resolve() already normalises the per-tick svg gid to
    // that group's canonical id, so reuse it here rather than letting two
    // different ticks pile up as separate selection entries.
    const rawId = g.id.slice(2);
    const id = resolve(rawId)?.id ?? rawId;
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
      items: movers.map((s) => ({
        id: s.id, panel: s.panel, obj: s.obj,
        baseSvg: dataToSvg(s.panel, s.obj.xy[0], s.obj.xy[1]),
      })),
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
      const [nx, ny] = svgToData(it.panel, it.baseSvg[0] + dx, it.baseSvg[1] + dy);
      it.obj.xy = [roundTo(nx, p.xlim), roundTo(ny, p.ylim)];
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
  if (sel.length === 1 && sel[0].draggable) {
    $('f-x').value = sel[0].obj.xy[0];
    $('f-y').value = sel[0].obj.xy[1];
  }
}

/* ------------------------------------------------------ keyboard */

document.addEventListener('keydown', (evt) => {
  if (evt.key === 'Escape') { setSelection([]); return; }

  if ((evt.ctrlKey || evt.metaKey) && evt.key.toLowerCase() === 'z') {
    evt.preventDefault();
    undo();
    return;
  }

  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
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
    const base = dataToSvg(s.panel, s.obj.xy[0], s.obj.xy[1]);
    const [nx, ny] = svgToData(s.panel, base[0] + a[0] * step, base[1] + a[1] * step);
    s.obj.xy = [roundTo(nx, p.xlim), roundTo(ny, p.ylim)];
  }
  showXY();
  reconcilePreviews();
  scheduleSave(250);
});

/* ------------------------------------------------------- field wiring */

$('f-text').addEventListener('input', (e) => {
  edit((o) => { o.text = e.target.value; }, { immediate: false });
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
