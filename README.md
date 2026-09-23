# FigForge

Drag-and-edit publication figures that stay reproducible as matplotlib code.

Runs two ways from the same code: **locally** (`python serve.py`, projects in
`figures/`, no account) or **hosted** on Vercel with invite-only accounts and
projects in a private Supabase Storage bucket. The first figure is the Q-circle
extraction (`qcircle_C_R_Goff.py`) for the 2 K Nb-plated Cu cavity; any CSV can
start a new figure.

## Run it

```bash
python -m venv .venv && .venv\Scripts\activate   # (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
python build.py     # CSV -> analysis -> spec.json -> figure.svg + figure.py
python serve.py     # opens http://127.0.0.1:8000
```

Locally the server binds `127.0.0.1` and needs no account. The code editor's
in-browser Run downloads Python (Pyodide) from jsDelivr the first time.

Hosted: `vercel.json` routes every URL to `api/index.py`, which runs the same
server. It needs `SUPABASE_URL` and `SUPABASE_SECRET_KEY` in the Vercel project
settings; sign-up is invite-only (the admin invites from the profile menu).

Tests: `pip install -r requirements-dev.txt`, then `python tests/run_all.py`
(see that file).

## What you can do

- **Click** any label to select it, or **click a panel's plot area** to select
  its axes (for xlim/ylim/scale/aspect — see below). Pick either from the
  Elements list too, which is how you reach labels hidden under others.
- **Ctrl-click** (Cmd on macOS) adds or removes one thing from the selection.
  **Drag a box** over empty canvas space to select everything under it — hold
  Ctrl while dragging to add the box's contents to the current selection
  instead of replacing it. Clicking empty space outside any panel deselects.
- **Drag** a label anywhere. Arrow keys nudge by 1 pt, Shift+arrows by 10.
- **Double-click** a label to jump straight into retyping it (the sidebar's
  text field, cursor pre-selected — there's no real editable text sitting
  in the figure itself, just matplotlib's rendered glyph shapes).
- **Retype** it, including matplotlib mathtext (`$\beta_1$`, `$Q_L$`); change
  its **size**, **color**, alignment, and whether it has a background box.
  A **Greek-letter/symbol palette** sits under the text field — click one to
  insert it at the cursor. It inserts the literal Unicode character, not a
  backslash command: matplotlib mathtext renders literal Unicode Greek
  directly, correctly, in or out of `$...$` math mode, so no LaTeX-command
  translation is needed.
- Standard text-editing shortcuts work normally inside the text field —
  **Ctrl+A/X/C/V** for select-all/cut/copy/paste, and **Ctrl+Z** undoes your
  last keystroke via the browser's own native text undo rather than
  reverting the whole figure (that only happens when a text field isn't
  focused).
- **Ctrl+Z** to undo. **Rebuild** discards all edits and regenerates from the CSV.
- **figure.py** downloads a standalone script that reproduces exactly what you
  see. **PNG** exports at the spec's dpi.

Panel titles and axis labels are editable (text/size/color) but not draggable —
matplotlib positions those itself.

### Editing a panel's axes

Click anywhere inside a panel's plot area (not on a label) to select the panel
itself. The Inspector switches to axis controls: **x/y min and max**, **x/y
scale** (linear or log), **aspect** (auto, or equal for a true 1:1 circle plot
like panels (a) and (b) here), **tick label size**, and **frame width**.

- Ctrl-click other panels to bulk-edit axes together — useful for e.g. giving
  several panels the same aspect. Limits stay per-panel even in a bulk edit
  (setting min doesn't overwrite each panel's own max).
- Switching to log scale is refused client-side, with an explanation, if the
  panel's current range on that axis touches zero or goes negative —
  matplotlib can't render that, so it's caught before the round-trip.
- The same panel selection also controls **tick label size** (x and y
  separately) and **frame width** (the spine linewidth around the plot box).
  Both are per-panel, so bulk-selecting several panels bumps all of them
  together the same way the label peer groups do.
- **Click a tick label directly** (one of the numbers along an axis) and
  every tick on that axis highlights as one group, ready to bulk-edit --
  no need to find empty plot area to click. A **Similar** chip offers
  *All x-axis ticks* / *All y-axis ticks* across every panel, same as the
  label peer groups. Ctrl-click a tick on a different panel's axis to add
  it to the selection; ctrl-clicking a different tick on an *already*
  selected axis toggles that whole axis back off, since every tick on one
  axis is always one selectable thing, never individually addressable --
  matplotlib regenerates the actual tick set on every limit change.
- The Inspector only shows fields for the axis (or axes) actually
  selected: clicking a y-tick shows just the Y axis section (min, max,
  scale, tick size) -- no X axis controls in sight. Clicking blank plot
  area, or selecting both an x-tick and a y-tick together, shows both
  axes; **Aspect** and **Frame width** only appear for a full panel
  selection, since neither belongs to one axis.

### Curves: style and their real data

Click a curve (or a marker/line construct like the origin cross or the
center dot) to select it. The Inspector shows its **legend label**,
**color**, **marker**, **line width**, **marker size**, and **opacity** -- writing to
the series' own `style` dict in spec.json, same as matplotlib's own
kwargs. `a_meas` already ships at 0.75 opacity by default, since a dense
scatter usually reads better slightly transparent. Below the figure, a scrollable table shows that curve's actual
(x, y) values, fetched from the same data your CSV loaded -- not
estimated, not AI-guessed, the real numbers. Ctrl-click curves in the
same or other panels to bulk-edit style via **All curves in panel (a)**
/ **All curves**; the table only shows for a single selected curve, since
two curves' data in one table doesn't mean anything.

A curve isn't draggable -- moving data doesn't mean anything -- and its
click target is deliberately not its bounding box: for a scatter that
spans most of the panel, a bbox hit-area would swallow clicks meant for
labels drawn on top of it. Instead the server draws an invisible,
decimated path tracing the curve's actual shape (confirmed to land
exactly on the real data, not approximated) with a wide but fully
transparent stroke -- genuinely clickable, since SVG hit-testing treats
an opacity-0 stroke as painted, unlike `stroke: none`. This sidesteps a
real limitation found while building it: matplotlib silently drops a
gid from any artist it rasterizes for performance (see Latency above),
which is exactly the several-thousand-point series that most need to
stay clickable -- so the click target is a separate, always-vector
artist, decoupled from how the curve is actually drawn.

Overlapping curves are a real case here, not hypothetical: panel (a)'s
fit line sits almost exactly on top of the data it was fit to, so their
click targets cover nearly the same area. Whichever curve is drawn last
wins a click at any shared point (matching normal visual stacking, since
hit-paths are inserted in the same order as their visible curves) --
which would otherwise make the one underneath permanently unreachable by
clicking. Clicking the same spot again, on whatever's already selected,
steps to the next thing down the stack instead -- click once for the fit
line, click the same spot again for the raw data underneath it.

### Editing an arrow

Click an arrow (like the `R`/`|Γ_off|` callouts on panels (a) and (b)) to
select it. The Inspector shows its **color**, **arrow style** (heads on
one end, both ends, or none), **line width**, and **head size**, writing
directly to the arrow's own fields in spec.json (`color`/`arrowstyle`/
`lw`/`mutation_scale`) -- there's no nested `style` dict here, unlike a
curve. Ctrl-click other arrows to bulk-edit via **All arrows in panel
(a)** / **All arrows**; arrows get their own dedicated peer-group bucket
rather than falling into the generic size-based grouping, since an arrow
has no `.size` field to compare against.

Unlike a curve, an arrow *is* draggable, and it has two distinct drag
modes:

- **Dragging its body** moves the whole arrow -- both endpoints shift by
  the same amount, so it translates rigidly without changing its length
  or angle. This gets an instant optimistic preview: an honest
  `translate()` applied to the arrow's SVG group the moment you start
  dragging, replaced by the real render a beat later.
- **Dragging either endpoint** (once the arrow is already the only thing
  selected -- selecting it shows small blue handles on both ends) resizes
  and rotates it, moving just that one point while the other stays put.
  This deliberately does *not* get a faked preview, since honestly
  reflecting a new angle and length means recomputing the arrowhead
  geometry, not just applying a transform -- it waits for the real
  render, same as any edit that isn't a rigid translate.

Clicking an endpoint handle before the arrow is the sole selection (e.g.
the very first click, or while several things are selected together)
just selects the arrow as a whole, the same as clicking its body --
matching how PowerPoint/Illustrator only let you grab a handle once
you've already selected the one shape it belongs to. Under the hood, an
arrow gets the same click-target treatment as a curve: an always-vector,
fully transparent hit-path along its body (plus a small invisible circle
at each end) laid on top of however the arrow itself is drawn, so it
stays clickable regardless of rendering mode.

### Editing a legend

Click a panel's legend box to select it. **Drag it anywhere** in the plot;
**font size** and a **boxed frame** toggle live in the Inspector, and
**Reset position** returns it to its default preset corner. Ctrl+Shift+>/<
resizes it too, same as any other selection.

A dragged legend is stored as an axes-fraction position (0-1 within the
panel box), not a data coordinate -- "where in the box" shouldn't jump
around if you later change the axis limits. The drop point lands exactly
on the legend's corner: matplotlib's default legend padding
(`borderaxespad`) is turned off for a dragged legend specifically, after
measuring a deterministic few-pixel gap it would otherwise leave between
where you drop it and where the box actually renders. A legend still on
its default preset keeps the normal padding.

Ctrl-click other panels' legends to bulk-edit them together via the **All
legends** peer group, same pattern as everything else.

### Editing a family of labels at once

Selecting one label at a time is the wrong unit of work when the actual problem
is "every axis label in this figure should be the same size" (spec §6.3).

Click any label and a **Similar** bar appears under the figure offering the
families it belongs to — *All y-axis labels*, *All labels in panel (a)*,
*Everything at 12pt*, *Everything in #2e8b57*. Click one and the whole family is
selected; then size, color, alignment and the box toggle apply to all of them.

- **Ctrl-click** a label in the figure, in the Elements list, or a family chip
  to add or remove it from the selection.
- The **Selected** row lists what you have; click any chip's × to drop it.
- **A+ / A−** bump every selected label by 1 pt *relative to its own size*, so a
  set that started at different sizes stays proportional. Typing an absolute
  size sets them all the same. Fields show `mixed` when the selection disagrees.
  **Ctrl+Shift+.** (i.e. Ctrl+Shift+>) and **Ctrl+Shift+,** (Ctrl+Shift+<) do
  the same thing from the keyboard, PowerPoint-style — and work on whatever
  kind of thing is selected: a free label, an axis title, or a tick-axis
  selection (bumping that axis's tick size specifically, leaving the other
  axis alone).
- Dragging or arrow-nudging moves the entire selection together.
- Retyping is offered only for a single label, since it has no sensible meaning
  for many at once.

## How it works

The figure is three synchronized representations, with the **SPEC as the pivot**:

```
  CODE  ◀── codegen ──  SPEC  ── render ──▶  VIEW (browser)
                         ▲                     │
                         └──── drag events ────┘
```

`figures/qcircle/spec.json` is the source of truth. Every interaction mutates
the SPEC, POSTs it, and swaps in the SVG matplotlib renders back. Nothing does
find-and-replace on Python source.

**Why SVG, not PNG.** Each editable text artist gets a `gid`, so matplotlib
writes it as `<g id="t_a_beta1">` — the browser can find that node and translate
it locally for an instant drag. matplotlib draws a text's background box
*inside* that same group, so the box travels with the glyphs.

### Latency

A matplotlib re-render is far too slow to sit in the interaction loop, so it
isn't in it. Edits apply to the SPEC and are **faked in the SVG immediately**;
the real render lands later and replaces it.

These previews are exact, not approximations. matplotlib emits each line of
text as `<g style="fill: COLOR" transform="translate(ax ay) scale(s -s)">` with
`s = fontsize/100`, and glyph advances, line spacing and box padding are all
linear in font size — so scaling that group about the text's anchor is
precisely what matplotlib itself would draw. Position, size and color are all
instant. Only **retyping** text has to wait, because laying out new glyphs
(mathtext especially) is matplotlib's job.

Previews are derived by diffing the live SPEC against the spec the on-screen
SVG was rendered from, which makes the whole thing stateless and
self-correcting: if a render lands while more edits are queued, the leftover
difference is simply re-applied on top.

The render itself went from **2.5 s to ~0.8 s**, and its payload from 1.94 MB
to 0.17 MB:

| | before | after |
|---|---|---|
| skip the redundant `canvas.draw()` | 2555 ms | 2033 ms |
| rasterize dense series (preview only) | | 1757 ms |
| cache the `tight_layout` result | | **825 ms** |
| SVG payload | 1.94 MB, 14175 `<use>` | 0.17 MB, 597 `<use>` |

- The SVG backend recomputes a style string **per marker**, so a few thousand
  scatter points cost seconds. Rasterizing them applies to the editor view
  only — exports stay fully vector, and layout/geometry are untouched.
- `tight_layout` costs a full text-measurement pass but never looks at
  free-floating `ax.text` artists, so label edits reuse a cached result. The
  cache key covers everything `tight_layout` does read (titles, axis labels,
  limits, tick sizes, figure size), so changing one of those still recomputes.
- Dropping `canvas.draw()` and calling `apply_aspect()` explicitly is
  geometry-identical; `savefig` was redrawing everything anyway.

Each of these was verified geometry-identical to the slow path (0.000000 px).

**The coordinate round-trip** (spec section 3.3) is the correctness linchpin.
Drags happen in pixels; matplotlib places text in data coords. The renderer
emits a geometry map — each panel's drawn box in SVG units plus its limits and
scales — and the browser inverts a dropped pixel back through it. The
round-trip is accurate to ~0.004 px, so labels don't drift when re-rendered.

matplotlib's SVG backend draws at 72 dpi, so 1 SVG unit = 1 point = 1 display
pixel; the only subtlety is that SVG y runs down and data y runs up.

## Layout

```
build.py                bootstrap a figure from raw data
serve.py                start the local editor
api/index.py            the same server as a Vercel Python function
figforge/
  analyze.py            CSV -> curve arrays + derived scalars (the Q-circle science)
  spec_builder.py       analysis -> initial SPEC for the Q-circle figure
  csvimport.py          any CSV + chosen x/y columns -> a new figure
  render.py             SPEC -> SVG with gids + geometry map
  codegen.py            SPEC -> standalone figure.py
  codesync.py           edited figure.py -> SPEC (parsed, never executed)
  ops.py                editing operations as Claude API tool definitions
  server.py             HTTP API (local and hosted)
  project.py            project storage: local folders or Supabase Storage
  auth.py               accounts on Supabase Auth (hosted only)
web/                    the editor (vanilla JS, no build step)
  code.html / code.js   the Sublime-style figure.py editor
  pyworker.js           runs figure.py in the browser (Pyodide)
  vendor/codemirror/    CodeMirror 5 (MIT), vendored
tests/                  run_all.py + the suites; tests/live needs .env
figures/qcircle/
  data/vna_sweep.csv    raw VNA measurement
  data/curves.npz       analysed arrays (generated)
  spec.json             the semantic layer — the thing you edit
  figure.svg            last render (generated)
  figure.py             standalone reproducer (generated)
```

`figure.py` imports nothing from FigForge. It needs numpy, matplotlib and
`data/curves.npz` next to it, and that's it.

## Not in this build yet

- **The AI assistant itself.** `figforge/ops.py` has the editing operations as
  Claude API tool definitions and `POST /api/ops/<project>` runs them; the
  chat loop that calls the model is the next step.
- Deleting a whole project; editing values in the data table; adding a curve
  to an existing figure (a CSV figure picks its curves when it's created).
- Syncing figure edits back into a saved hand-edited `figure.py` (code →
  figure is synced; figure → your saved code shows a "figure changed" banner).
- A phone layout.
