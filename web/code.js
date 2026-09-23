/* FigForge code editor: figure.py in a Sublime-style editor.
 *
 * Opened from the figure editor's "figure.py" menu as code.html?project=X.
 * Shows the user's saved hand-edited figure.py if there is one, otherwise the
 * code generated from the current figure. Save keeps an edited copy with the
 * project (it never changes the draggable figure); Run executes the script
 * in this browser via Pyodide (see pyworker.js) -- never on the server.
 */

const $ = (id) => document.getElementById(id);
const PROJECT = new URLSearchParams(location.search).get('project') || '';

let generated = '';      // code generated from the figure's current spec
let rev = null;          // that spec's revision
let baseRev = null;      // rev the editor content was forked from
let savedValue = '';     // what's on the server, for the dirty marker
let hasCustom = false;   // an edited version is saved
let npz = null;          // data/curves.npz bytes, fetched on first Run
let worker = null;
let running = false;
let runsFinished = 0;    // completed runs (lets tests wait for a fresh result)

/* ------------------------------------------------------------- editor */

CodeMirror.commands.save = () => save();

const cm = CodeMirror($('editor'), {
  value: '',
  mode: 'python',
  theme: 'monokai',
  keyMap: 'sublime',
  lineNumbers: true,
  indentUnit: 4,
  tabSize: 4,
  indentWithTabs: false,
  styleActiveLine: true,
  matchBrackets: true,
  autoCloseBrackets: true,
  showCursorWhenSelecting: true,
  // normalizeKeyMap: CodeMirror only matches modifiers in its canonical
  // order (Shift-Cmd-Ctrl-Alt), so "Ctrl-Shift-P" would silently never fire.
  extraKeys: CodeMirror.normalizeKeyMap({
    'Ctrl-B': () => run(), 'Cmd-B': () => run(),
    'Ctrl-S': () => save(), 'Cmd-S': () => save(),
    'Ctrl-Shift-P': () => openPalette(), 'Cmd-Shift-P': () => openPalette(),
    F1: () => openPalette(),  // for browsers that keep Ctrl+Shift+P for themselves
    'Ctrl-Shift-B': () => run(),
    'Ctrl-`': () => togglePanel(), 'Esc': () => { if (!$('panel').hidden) togglePanel(false); },
    // Spaces, never tab characters, as Python expects.
    Tab: (c) => c.somethingSelected() ? c.indentSelection('add')
                                      : c.replaceSelection(' '.repeat(c.getOption('indentUnit'))),
  }),
});

function setStatus(msg, cls = '') {
  $('status-msg').textContent = msg;
  $('status-msg').className = cls;
  if (msg && !cls) setTimeout(() => { if ($('status-msg').textContent === msg) setStatus(''); }, 4000);
}

function refreshDirty() {
  const dirty = cm.getValue() !== savedValue;
  $('tab').classList.toggle('dirty', dirty);
  document.title = `${dirty ? '● ' : ''}figure.py — ${PROJECT}`;
  $('status-source').textContent = hasCustom || dirty ? 'Your edited version' : 'Generated from the figure';
}

cm.on('change', () => { refreshDirty(); scheduleMinimap(); });
cm.on('cursorActivity', () => {
  const sels = cm.listSelections();
  const c = cm.getCursor();
  $('status-pos').textContent = sels.length > 1
    ? `${sels.length} selection regions`
    : `Line ${c.line + 1}, Column ${c.ch + 1}`;
});
cm.on('scroll', () => drawMinimap());

window.addEventListener('beforeunload', (e) => {
  if (cm.getValue() !== savedValue) { e.preventDefault(); e.returnValue = ''; }
});

/* ------------------------------------------------------------ loading */

function block(html) {
  $('blocker-card').innerHTML = html;
  $('blocker').hidden = false;
}

function showBanner() {
  const b = $('banner');
  if (hasCustom && baseRev != null && rev != null && baseRev !== rev) {
    b.innerHTML = `The figure has been edited in FigForge since you saved this code ` +
      `(rev ${baseRev} → ${rev}), and your version doesn't include those changes. ` +
      `<button id="banner-gen">Open the generated code instead</button>`;
    b.hidden = false;
    $('banner-gen').onclick = () => loadGenerated();
  } else {
    b.hidden = true;
  }
}

async function load() {
  if (!/^[A-Za-z0-9_-]+$/.test(PROJECT)) {
    block('No project given. Open the code from the <b>figure.py</b> menu in <a href="/">FigForge</a>.');
    return;
  }
  const r = await fetch(`/api/script/${PROJECT}`);
  if (r.status === 401) {
    block('You’re signed out. <a href="/" target="_blank">Log in to FigForge</a>, then reload this tab.');
    return;
  }
  if (!r.ok) {
    block(`Project “${PROJECT}” wasn’t found. <a href="/">Back to FigForge</a>`);
    return;
  }
  const d = await r.json();
  generated = d.generated;
  rev = d.rev;
  hasCustom = !!d.custom;
  savedValue = hasCustom ? d.custom.code : generated;
  baseRev = hasCustom ? d.custom.base_rev : rev;
  cm.setValue(savedValue);
  cm.clearHistory();
  $('tree-project').textContent = PROJECT;
  $('side-note').innerHTML = `Run executes this script <b>in your browser</b> (Python via Pyodide); ` +
    `nothing runs on the server. Saving keeps your edited copy with the project — ` +
    `the draggable figure isn’t changed. <br><br><a href="/?project=${encodeURIComponent(PROJECT)}">` +
    `← Back to the figure editor</a>`;
  refreshDirty();
  showBanner();
  drawMinimap();
  cm.focus();
}

function loadGenerated() {
  cm.setValue(generated);
  baseRev = rev;
  $('banner').hidden = true;
  setStatus('Loaded the code generated from the current figure. Save to keep it.');
}

/* ------------------------------------------------------ save / reset */

async function save() {
  const code = cm.getValue();
  try {
    const r = await fetch(`/api/script/${PROJECT}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, base_rev: baseRev }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    savedValue = code;
    hasCustom = true;
    refreshDirty();
    showBanner();
    setStatus('Saved');
  } catch (e) {
    setStatus(`Save failed: ${e.message}`, 'err');
  }
}

async function resetToGenerated() {
  if (hasCustom && !confirm('Discard your edited figure.py and go back to the generated code?')) return;
  const r = await fetch(`/api/script/${PROJECT}/reset`, { method: 'POST' });
  if (!r.ok) { setStatus('Reset failed', 'err'); return; }
  hasCustom = false;
  savedValue = generated;
  baseRev = rev;
  cm.setValue(generated);
  showBanner();
  refreshDirty();
  setStatus('Back to the generated code');
}

function download() {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([cm.getValue()], { type: 'text/x-python' }));
  a.download = `${PROJECT}_figure.py`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* --------------------------------------------------------------- run */

function togglePanel(show) {
  const p = $('panel');
  p.hidden = show === undefined ? !p.hidden : !show;
  cm.refresh();
  drawMinimap();
}

function panelState(text, cls) {
  $('panel-state').textContent = text;
  $('panel-state').className = 'panel-state ' + (cls || '');
}

let errMark = null;
function markError(line) {
  if (errMark != null) cm.removeLineClass(errMark, 'background', 'cm-error-line');
  errMark = null;
  if (line) {
    errMark = line - 1;
    cm.addLineClass(errMark, 'background', 'cm-error-line');
    cm.scrollIntoView({ line: errMark, ch: 0 }, 120);
  }
}

function newWorker() {
  worker = new Worker('pyworker.js', { type: 'module' });
  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === 'status') { panelState(m.text, 'run'); return; }
    running = false;
    runsFinished++;
    $('btn-run').textContent = '▶ Run';
    const con = $('console');
    const out = m.out.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    con.innerHTML = (out || '<span class="info">(no output)</span>') +
      (m.ok ? '' : '\n<span class="err">[Finished with an error]</span>');
    $('figures').innerHTML = m.images.map((b) => `<img src="data:image/png;base64,${b}" alt="figure output">`).join('');
    panelState(m.ok ? `Finished in ${((performance.now() - runStart) / 1000).toFixed(1)} s` +
               ` · ${m.images.length} figure${m.images.length === 1 ? '' : 's'}` : 'Error', m.ok ? 'ok' : 'err');
    markError(m.ok ? null : m.errLine);
  };
}

let runStart = 0;
/** Ctrl+B, like Sublime's Build: while a run is still going, it's stopped and
 *  the current code runs fresh. (The ■ Cancel button only stops.) */
async function run() {
  if (running) cancelRun();
  togglePanel(true);
  panelState('Starting…', 'run');
  $('console').innerHTML = '';
  $('figures').innerHTML = '';
  markError(null);
  try {
    if (!npz) {
      panelState('Fetching data/curves.npz…', 'run');
      const r = await fetch(`/api/npz/${PROJECT}`);
      if (!r.ok) throw new Error(r.status === 401 ? 'signed out — log in again' : r.statusText);
      npz = await r.arrayBuffer();
    }
  } catch (e) {
    panelState('Error', 'err');
    $('console').innerHTML = `<span class="err">Couldn’t load the project data: ${e.message}</span>`;
    return;
  }
  if (!worker) newWorker();
  running = true;
  runStart = performance.now();
  $('btn-run').textContent = '■ Cancel';
  worker.postMessage({ type: 'run', code: cm.getValue(), npz: npz.slice(0) });
}

function cancelRun() {
  if (!running) return;
  worker.terminate();   // the only way to stop Python mid-run; the next Run reboots it
  worker = null;
  running = false;
  $('btn-run').textContent = '▶ Run';
  panelState('Cancelled', 'err');
}

// Drag the panel's top edge to resize it, as in Sublime.
$('panel-resize').addEventListener('pointerdown', (e) => {
  const panel = $('panel');
  const startY = e.clientY, startH = panel.offsetHeight;
  const move = (ev) => {
    panel.style.height = Math.max(90, Math.min(window.innerHeight - 160, startH + startY - ev.clientY)) + 'px';
    cm.refresh();
  };
  const up = () => { removeEventListener('pointermove', move); removeEventListener('pointerup', up); drawMinimap(); };
  addEventListener('pointermove', move);
  addEventListener('pointerup', up);
});
$('panel-close').onclick = () => togglePanel(false);

/* ----------------------------------------------------------- minimap */
// Sublime's zoomed-out code overview: every line drawn as tiny coloured
// runs, the visible region boxed, click or drag to jump.

const TOKEN_COLOURS = {
  keyword: '#f92672', string: '#e6db74', number: '#ae81ff', comment: '#75715e',
  def: '#a6e22e', builtin: '#66d9ef', 'variable-2': '#66d9ef', operator: '#f92672',
  property: '#a6e22e', meta: '#a6e22e', atom: '#ae81ff',
};
const MM_LINE = 3, MM_CHAR = 1.15;
let minimapTimer = null;
let minimapCache = null;

function scheduleMinimap() {
  clearTimeout(minimapTimer);
  minimapTimer = setTimeout(() => { minimapCache = null; drawMinimap(); }, 120);
}

function renderMinimapCache(width) {
  const lines = cm.lineCount();
  const c = document.createElement('canvas');
  const dpr = window.devicePixelRatio || 1;
  c.width = width * dpr;
  c.height = Math.max(1, lines * MM_LINE) * dpr;
  const g = c.getContext('2d');
  g.scale(dpr, dpr);
  for (let i = 0; i < lines; i++) {
    let x = 6;
    for (const t of cm.getLineTokens(i, true)) {
      const text = t.string;
      const lead = text.length - text.trimStart().length;
      const body = text.trim().length;
      if (body) {
        const type = (t.type || '').split(' ')[0];
        g.fillStyle = TOKEN_COLOURS[type] || '#cfcfc2';
        g.globalAlpha = type ? 0.85 : 0.55;
        g.fillRect(x + lead * MM_CHAR, i * MM_LINE, body * MM_CHAR, MM_LINE - 1);
      }
      x += text.length * MM_CHAR;
    }
  }
  return c;
}

function minimapGeometry() {
  const canvas = $('minimap');
  const h = canvas.clientHeight;
  const info = cm.getScrollInfo();
  const contentH = cm.lineCount() * MM_LINE;
  const lineH = cm.defaultTextHeight();
  const firstLine = info.top / lineH;
  const visLines = info.clientHeight / lineH;
  // When the overview is taller than the panel it scrolls along with the code.
  const maxScroll = Math.max(1, info.height - info.clientHeight);
  const offset = Math.max(0, contentH - h) * Math.min(1, info.top / maxScroll);
  return { canvas, h, contentH, firstLine, visLines, offset };
}

function drawMinimap() {
  const canvas = $('minimap');
  if (!canvas.offsetParent) return;
  const w = canvas.clientWidth, dpr = window.devicePixelRatio || 1;
  if (canvas.width !== w * dpr || canvas.height !== canvas.clientHeight * dpr) {
    canvas.width = w * dpr;
    canvas.height = canvas.clientHeight * dpr;
  }
  if (!minimapCache) minimapCache = renderMinimapCache(w);
  const { h, firstLine, visLines, offset } = minimapGeometry();
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  g.drawImage(minimapCache, 0, -offset, w, minimapCache.height / dpr);
  g.fillStyle = 'rgba(255, 255, 255, 0.07)';
  g.fillRect(0, firstLine * MM_LINE - offset, w, visLines * MM_LINE);
}

function minimapJump(e) {
  const { canvas, offset, visLines } = minimapGeometry();
  const y = e.clientY - canvas.getBoundingClientRect().top + offset;
  const line = y / MM_LINE - visLines / 2;
  cm.scrollTo(null, Math.max(0, line) * cm.defaultTextHeight());
}
$('minimap').addEventListener('pointerdown', (e) => {
  minimapJump(e);
  const move = (ev) => minimapJump(ev);
  const up = () => { removeEventListener('pointermove', move); removeEventListener('pointerup', up); };
  addEventListener('pointermove', move);
  addEventListener('pointerup', up);
});
addEventListener('resize', () => { minimapCache = null; drawMinimap(); });

/* ---------------------------------------------------- command palette */

const COMMANDS = [
  { name: 'Build: Run figure.py', key: 'Ctrl+B', run: () => run() },
  { name: 'Build: Cancel', key: '', run: () => cancelRun() },
  { name: 'File: Save', key: 'Ctrl+S', run: () => save() },
  { name: 'File: Download figure.py', key: '', run: () => download() },
  { name: 'File: Reset to Generated Code', key: '', run: () => resetToGenerated() },
  { name: 'File: Open Generated Code (keep editing)', key: '', run: () => loadGenerated() },
  { name: 'Find', key: 'Ctrl+F', run: () => cm.execCommand('find') },
  { name: 'Replace', key: 'Ctrl+H', run: () => cm.execCommand('replace') },
  { name: 'Go to Line', key: 'Ctrl+G', run: () => cm.execCommand('jumpToLine') },
  { name: 'Toggle Comment', key: 'Ctrl+/', run: () => cm.execCommand('toggleCommentIndented') },
  { name: 'View: Toggle Output Panel', key: 'Ctrl+`', run: () => togglePanel() },
  { name: 'View: Toggle Side Bar', key: '', run: () => { $('sidebar').hidden = !$('sidebar').hidden; cm.refresh(); } },
  { name: 'View: Toggle Minimap', key: '', run: () => { $('minimap').hidden = !$('minimap').hidden; cm.refresh(); drawMinimap(); } },
  { name: 'FigForge: Back to the Figure Editor', key: '', run: () => { location.href = `/?project=${encodeURIComponent(PROJECT)}`; } },
];
let paletteItems = [], paletteIndex = 0;

/** Sublime-style fuzzy match: every typed character, in order. */
function fuzzy(query, text) {
  const q = query.toLowerCase(), t = text.toLowerCase();
  const hits = [];
  let j = 0;
  for (let i = 0; i < t.length && j < q.length; i++) if (t[i] === q[j]) { hits.push(i); j++; }
  return j === q.length ? hits : null;
}

function renderPalette() {
  const q = $('palette-input').value.replace(/\s+/g, '');
  paletteItems = COMMANDS.map((c) => ({ c, hits: fuzzy(q, c.name) })).filter((x) => x.hits);
  paletteIndex = Math.min(paletteIndex, Math.max(0, paletteItems.length - 1));
  $('palette-list').innerHTML = paletteItems.map(({ c, hits }, i) => {
    const name = [...c.name].map((ch, k) => (hits.includes(k) ? `<b>${ch}</b>` : ch)).join('');
    return `<li class="${i === paletteIndex ? 'on' : ''}" data-i="${i}"><span>${name}</span><span class="key">${c.key}</span></li>`;
  }).join('');
}

function openPalette() {
  $('palette').hidden = false;
  $('palette-input').value = '';
  paletteIndex = 0;
  renderPalette();
  $('palette-input').focus();
}

function closePalette() { $('palette').hidden = true; cm.focus(); }

function runPaletteItem(i) {
  const item = paletteItems[i];
  closePalette();
  if (item) item.c.run();
}

$('palette-input').addEventListener('input', () => { paletteIndex = 0; renderPalette(); });
$('palette-input').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown') { paletteIndex = Math.min(paletteIndex + 1, paletteItems.length - 1); renderPalette(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { paletteIndex = Math.max(paletteIndex - 1, 0); renderPalette(); e.preventDefault(); }
  else if (e.key === 'Enter') { runPaletteItem(paletteIndex); e.preventDefault(); }
  else if (e.key === 'Escape') { closePalette(); e.preventDefault(); }
});
$('palette-list').addEventListener('click', (e) => {
  const li = e.target.closest('li');
  if (li) runPaletteItem(+li.dataset.i);
});
document.addEventListener('pointerdown', (e) => {
  if (!$('palette').hidden && !$('palette').contains(e.target) && e.target !== $('btn-palette')) closePalette();
});
// Also reachable while focus is outside the editor.
document.addEventListener('keydown', (e) => {
  if (cm.hasFocus()) return;  // the editor's own extraKeys already handled it
  const mod = e.ctrlKey || e.metaKey;
  if ((mod && e.shiftKey && e.key.toLowerCase() === 'p') || e.key === 'F1') { e.preventDefault(); openPalette(); }
  else if (mod && !e.shiftKey && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
  else if (mod && !e.shiftKey && e.key.toLowerCase() === 'b') { e.preventDefault(); run(); }
});

/* ------------------------------------------------------------ buttons */

$('btn-run').onclick = () => (running ? cancelRun() : run());
$('btn-save').onclick = () => save();
$('btn-download').onclick = () => download();
$('btn-palette').onclick = () => openPalette();

load();
