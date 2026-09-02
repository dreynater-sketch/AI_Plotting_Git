/* FigForge editor — click, retype, resize, recolor and drag figure labels.
 *
 * The SPEC is the source of truth. Every interaction here does the same thing:
 * mutate the SPEC, POST it, and swap in the SVG matplotlib renders back. Drags
 * are applied locally first (an SVG transform on the gid group) so they feel
 * instant, then confirmed by the real render.
 *
 * No AI, no external calls. Everything goes to 127.0.0.1.
 */

const FIGURE = 'qcircle';

let spec = null;
let geometry = null;
let svgEl = null;
let selectedId = null;
let history = [];
let zoom = 100;

const $ = (id) => document.getElementById(id);
const canvas = $('canvas');

/* ------------------------------------------------------------- plumbing */

function setStatus(msg, cls = '') {
  const el = $('status');
  el.textContent = msg;
  el.className = 'status ' + cls;
}

const clone = (o) => JSON.parse(JSON.stringify(o));

function pushHistory() {
  history.push(clone(spec));
  if (history.length > 50) history.shift();
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
    spec = data.spec;
    geometry = data.geometry;
    applySvg(data.svg);
    setStatus(`saved · rev ${spec.rev}`);
  } catch (e) {
    setStatus(e.message, 'err');
  } finally {
    inFlight = false;
    if (needsSave) { needsSave = false; scheduleSave(0); }
  }
}

/* ------------------------------------------------- spec element lookup */

function panelById(pid) {
  return spec.panels.find((p) => p.id === pid);
}

/** Map an SVG element id (minus the "t_" prefix) to its place in the SPEC. */
function resolve(id) {
  if (!id) return null;
  if (id === 'suptitle') {
    return { kind: 'suptitle', obj: spec.suptitle, draggable: false, panel: null };
  }
  const m = id.match(/^(.+)__(title|xlabel|ylabel)$/);
  if (m) {
    const p = panelById(m[1]);
    if (!p || !p[m[2]]) return null;
    return { kind: m[2], obj: p[m[2]], draggable: false, panel: p.id };
  }
  for (const p of spec.panels) {
    for (const t of p.texts || []) {
      if (t.id === id) return { kind: 'text', obj: t, draggable: true, panel: p.id };
    }
  }
  return null;
}

/* ------------------------------------------- coordinate transformations */

function invAxis(lim, scale, f) {
  if (scale === 'log') {
    const l0 = Math.log10(lim[0]), l1 = Math.log10(lim[1]);
    return Math.pow(10, l0 + f * (l1 - l0));
  }
  return lim[0] + f * (lim[1] - lim[0]);
}

/** SVG user units -> data coords for a panel (spec section 3.3). */
function svgToData(panelId, x, y) {
  const g = geometry.panels[panelId];
  const [bx, by, bw, bh] = g.bbox;
  const fx = (x - bx) / bw;
  const fy = (by + bh - y) / bh;   // SVG y runs down, data y runs up
  return [invAxis(g.xlim, g.xscale, fx), invAxis(g.ylim, g.yscale, fy)];
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
    // Non-draggable text (titles, axis labels) gets a different cursor.
    if (!r.draggable) g.classList.add('ff-static');
    addHitTarget(g);
  }
  if (selectedId) drawOutline(selectedId);
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

function applyZoom() {
  if (!svgEl || !geometry) return;
  svgEl.style.width = (geometry.width * zoom / 100) + 'px';
  svgEl.style.height = (geometry.height * zoom / 100) + 'px';
}

function groupFor(id) {
  return svgEl ? svgEl.querySelector(`g[id="t_${CSS.escape(id)}"]`) : null;
}

function clearOutline() {
  svgEl?.querySelectorAll('.ff-outline').forEach((n) => n.remove());
}

/** Selection box, inserted as a sibling of the target so it shares its space. */
function drawOutline(id) {
  clearOutline();
  const g = groupFor(id);
  if (!g) return;
  let bb;
  try { bb = g.getBBox(); } catch { return; }
  const pad = 3;
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('class', 'ff-outline');
  rect.setAttribute('x', bb.x - pad);
  rect.setAttribute('y', bb.y - pad);
  rect.setAttribute('width', bb.width + 2 * pad);
  rect.setAttribute('height', bb.height + 2 * pad);
  rect.setAttribute('fill', 'none');
  rect.setAttribute('stroke', '#1f6feb');
  rect.setAttribute('stroke-width', '1');
  rect.setAttribute('stroke-dasharray', '4 3');
  rect.setAttribute('pointer-events', 'none');
  const tf = g.getAttribute('transform');
  if (tf) rect.setAttribute('transform', tf);
  g.parentNode.insertBefore(rect, g.nextSibling);
}

/* ------------------------------------------------------------ inspector */

function select(id) {
  selectedId = id;
  const r = resolve(id);
  if (!r) { deselect(); return; }

  $('insp-empty').hidden = true;
  $('insp-body').hidden = false;
  $('insp-kind').textContent = r.kind === 'text' ? 'label' : r.kind;
  $('insp-id').textContent = id;

  $('f-text').value = r.obj.text ?? '';
  $('f-size').value = r.obj.size ?? 12;
  const color = normHex(r.obj.color ?? '#000000');
  $('f-color').value = color;
  $('f-color-hex').value = color;

  const posGroup = $('pos-group');
  posGroup.hidden = !r.draggable;
  if (r.draggable) {
    const p = panelById(r.panel);
    $('f-x').value = r.obj.xy[0];
    $('f-y').value = r.obj.xy[1];
    $('f-xunit').textContent = `(${stripMath(p.xlabel?.text) || 'data'})`;
    $('f-yunit').textContent = `(${stripMath(p.ylabel?.text) || 'data'})`;
    $('f-ha').value = r.obj.ha ?? 'left';
    $('f-va').value = r.obj.va ?? 'baseline';
    $('f-box').checked = !!r.obj.bbox;
  }

  drawOutline(id);
  markList();
}

function deselect() {
  selectedId = null;
  $('insp-empty').hidden = false;
  $('insp-body').hidden = true;
  clearOutline();
  markList();
}

function normHex(c) {
  if (typeof c !== 'string') return '#000000';
  if (/^#[0-9a-f]{6}$/i.test(c)) return c.toLowerCase();
  if (c === 'black') return '#000000';
  if (c === 'white') return '#ffffff';
  // matplotlib grey strings like "0.15"
  const g = parseFloat(c);
  if (!Number.isNaN(g) && g >= 0 && g <= 1 && /^[\d.]+$/.test(c)) {
    const v = Math.round(g * 255).toString(16).padStart(2, '0');
    return `#${v}${v}${v}`;
  }
  return '#000000';
}

const stripMath = (s) => (s || '').replace(/\$/g, '').replace(/\\[a-zA-Z]+/g, '').trim();

/** Edit the selected element, then persist. */
function edit(fn, { immediate = true } = {}) {
  const r = resolve(selectedId);
  if (!r) return;
  pushHistory();
  fn(r.obj, r);
  scheduleSave(immediate ? 0 : 350);
}

/* --------------------------------------------------------- element list */

function buildList() {
  const list = $('element-list');
  list.innerHTML = '';
  const add = (label, id, tag) => {
    const b = document.createElement('button');
    b.dataset.eid = id;
    b.innerHTML = `<span class="tag">${tag}</span>${escapeHtml(label)}`;
    b.title = label;
    b.onclick = () => select(id);
    list.appendChild(b);
  };
  const group = (name) => {
    const d = document.createElement('div');
    d.className = 'group';
    d.textContent = name;
    list.appendChild(d);
  };

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

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function markList() {
  for (const b of $('element-list').querySelectorAll('button')) {
    b.classList.toggle('on', b.dataset.eid === selectedId);
  }
}

/* --------------------------------------------------------------- drag */

let drag = null;

canvas.addEventListener('pointerdown', (evt) => {
  const g = evt.target.closest('g[id^="t_"]');
  if (!g) { deselect(); return; }
  const id = g.id.slice(2);
  select(id);

  const r = resolve(id);
  if (!r || !r.draggable) return;

  evt.preventDefault();
  const start = clientToSvg(evt);
  drag = {
    id, group: g, panel: r.panel,
    start,
    anchor: geometry.texts[id].anchor,
    moved: false,
  };
  g.classList.add('ff-dragging');
  svgEl.setPointerCapture(evt.pointerId);
});

canvas.addEventListener('pointermove', (evt) => {
  if (!drag) return;
  const now = clientToSvg(evt);
  const dx = now.x - drag.start.x;
  const dy = now.y - drag.start.y;
  if (!drag.moved && Math.hypot(dx, dy) < 1.5) return;
  drag.moved = true;
  drag.dx = dx;
  drag.dy = dy;
  const tf = `translate(${dx} ${dy})`;
  drag.group.setAttribute('transform', tf);
  svgEl.querySelector('.ff-outline')?.setAttribute('transform', tf);
});

canvas.addEventListener('pointerup', (evt) => {
  if (!drag) return;
  const d = drag;
  drag = null;
  d.group.classList.remove('ff-dragging');
  try { svgEl.releasePointerCapture(evt.pointerId); } catch { /* already gone */ }
  if (!d.moved) return;

  const p = geometry.panels[d.panel];
  const [nx, ny] = svgToData(d.panel, d.anchor[0] + d.dx, d.anchor[1] + d.dy);
  const r = resolve(d.id);
  pushHistory();
  r.obj.xy = [roundTo(nx, p.xlim), roundTo(ny, p.ylim)];
  if (selectedId === d.id) {
    $('f-x').value = r.obj.xy[0];
    $('f-y').value = r.obj.xy[1];
  }
  scheduleSave(0);
});

canvas.addEventListener('pointercancel', () => {
  if (drag) { drag.group.classList.remove('ff-dragging'); drag = null; }
});

/* ------------------------------------------------------ keyboard nudge */

document.addEventListener('keydown', (evt) => {
  if (evt.key === 'Escape') { deselect(); return; }

  if ((evt.ctrlKey || evt.metaKey) && evt.key.toLowerCase() === 'z') {
    evt.preventDefault();
    undo();
    return;
  }

  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if (typing || !selectedId) return;

  const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  const a = arrows[evt.key];
  if (!a) return;
  const r = resolve(selectedId);
  if (!r || !r.draggable) return;

  evt.preventDefault();
  const step = evt.shiftKey ? 10 : 1;           // SVG units == points
  const anchor = geometry.texts[selectedId].anchor;
  const p = geometry.panels[r.panel];
  const [nx, ny] = svgToData(r.panel, anchor[0] + a[0] * step, anchor[1] + a[1] * step);
  pushHistory();
  r.obj.xy = [roundTo(nx, p.xlim), roundTo(ny, p.ylim)];
  $('f-x').value = r.obj.xy[0];
  $('f-y').value = r.obj.xy[1];

  // Show the nudge immediately; the render confirms it a moment later.
  const g = groupFor(selectedId);
  const tf = `translate(${a[0] * step} ${a[1] * step})`;
  g?.setAttribute('transform', tf);
  svgEl.querySelector('.ff-outline')?.setAttribute('transform', tf);
  scheduleSave(180);
});

/* ------------------------------------------------------- field wiring */

$('f-text').addEventListener('input', (e) => {
  edit((o) => { o.text = e.target.value; }, { immediate: false });
});
$('f-size').addEventListener('input', (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isFinite(v) && v > 0) edit((o) => { o.size = v; }, { immediate: false });
});
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
  edit((o) => {
    if (e.target.checked) {
      o.bbox = { boxstyle: 'round,pad=0.25', fc: 'white', ec: '#999999', lw: 0.8 };
    } else {
      delete o.bbox;
    }
  });
});

/* ------------------------------------------------------------ toolbar */

function undo() {
  if (!history.length) return;
  spec = history.pop();
  $('btn-undo').disabled = !history.length;
  if (selectedId) select(selectedId);
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
    geometry = data.geometry;
    deselect();
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
