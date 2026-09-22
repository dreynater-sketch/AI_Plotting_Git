# FigForge — Progress Log

What's been built so far, in the order it was built, and why each piece
exists. FigForge is a local-only editor that makes a matplotlib figure
draggable and editable in a browser while staying reproducible as
version-controlled Python — no AI, no API keys, no network calls anywhere in
this build (that's a deliberate, later phase). Everything below runs against
one real test figure: `qcircle_C_R_Goff.py`, a Q-circle extraction for a 2 K
Nb-plated Cu cavity, three panels.

## Architecture (established in the MVP, unchanged since)

**SPEC is the pivot.** `figures/qcircle/spec.json` is the single source of
truth. CODE and VIEW both derive from it, never from each other:

```
analyze.py       CSV -> curve arrays + derived scalars (the science, untouched)
spec_builder.py  analysis -> the initial SPEC reproducing the original figure
render.py        SPEC -> SVG, with a gid on every editable artist + a geometry map
codegen.py       SPEC -> a standalone figure.py (imports nothing from FigForge)
server.py        local-only HTTP API on 127.0.0.1
web/             vanilla-JS editor, no build step, no framework
```

Every interaction mutates the SPEC, POSTs it, and swaps in the SVG that comes
back. Nothing does find-and-replace on Python source, and nothing about the
editor is needed to reproduce the figure later — `figure.py` only needs numpy,
matplotlib, and the analysed data array next to it.

**The coordinate round-trip is the correctness linchpin.** Drags happen in
screen pixels; matplotlib places things in data coordinates. The renderer
emits a geometry map (each panel's drawn box in SVG units, plus its limits and
scales) and the browser inverts a dropped pixel back through it — accurate to
~0.004 px, confirmed empirically, not assumed. This one mechanism is what
lets every later feature (ticks, legends, curves, arrows) add a new kind of
draggable thing without reinventing how dragging works.

## What's shipped, roughly chronologically

**MVP** — click, drag, retype (mathtext included), resize, recolor any free
label; arrow keys nudge; Ctrl+Z undoes; a full rebuild discards edits and
regenerates from the CSV. Labels dragged past the axes edge render in full
rather than clipping. Codegen made idempotent (no timestamp in the output) so
an unchanged spec produces a byte-identical `figure.py`.

**Made it feel instant** — the first real render took 2.5s, which made every
edit feel broken. Fixed in two parts: edits now apply to the SPEC and are
faked in the SVG immediately (an exact reproduction of what matplotlib itself
would draw — text scales as `translate() scale(fontsize/100)`, so scaling that
group about its anchor *is* correct, not an approximation), with the real
render replacing the fake a few hundred ms later. Then the render itself got
3x faster (825ms, down from 2555ms; 0.17MB payload, down from 1.94MB) by
dropping a redundant canvas draw, rasterizing only the editor's dense-series
preview (exports stay fully vector), and caching the layout pass. Every
speedup was verified geometry-identical to the slow path before being kept.

**Selecting and editing at scale** — the real unit of work is usually "every
axis label should be the same size," not one label at a time. Added
Ctrl-click multi-select, Shift-click-to-toggle, a rubber-band box select, and
per-selection "family" chips (All y-axis labels, All labels in panel (a),
Everything at 12pt, Everything in #2e8b57) that select a whole matching group
in one click. A+/A- bump every selected label relative to its own size so a
mixed-size set stays proportional. Ctrl+Shift+>/< adds PowerPoint's
grow/shrink-text shortcut on top of the same mechanism.

**Panels, axes, ticks, and the frame** — clicking blank plot area (not a
label) selects the panel itself: x/y min/max, scale, aspect, tick label size,
and frame width, all bulk-editable across multiple panels while each keeps
its own limits. Clicking an individual tick number selects and highlights the
*whole axis* it belongs to as one group (tick labels get an indexed gid purely
for click resolution — they're never persisted as SPEC entities, since
matplotlib regenerates the actual tick set on every limit change). The
Inspector narrows itself to only the fields relevant to what's actually
selected, rather than always showing every axis control. Switching to log
scale is refused client-side, with an explanation, when the panel's current
range isn't strictly positive.

**Editing UX polish** — fixed Ctrl+Z hijacking a text field's native undo
(the "am I typing" guard was checked after the undo branch, not before).
Double-click a label jumps straight into retyping it. Added a Greek-letter/
math-symbol palette, confirmed empirically that matplotlib mathtext renders
literal Unicode Greek correctly in both math and plain-text mode. Tick-size
changes got their own instant preview after benchmarking showed they were the
one quick-adjustment path with no feedback at all (~1s round trip, invisible
until the real render landed).

**Legends** — click-select, drag anywhere, font size, frame toggle, a
reset-to-preset button, and cross-panel bulk edit. Position is stored as an
axes-fraction (0–1 within the panel box), a second coordinate system
alongside the existing data-coordinate one, added by generalizing
`dataToSvg`/`svgToData` to take a `coords` parameter rather than duplicating
the math. Measured (not assumed) that legend position/size/frame don't affect
the layout pass at all, and that matplotlib's default anchor padding needed
turning off for a dragged legend to land exactly where it was dropped.

**Curves and their real data** — click a curve to edit its legend label,
color, marker, line width, marker size, and opacity, writing straight into
matplotlib's own style kwargs. Selecting one curve shows its actual (x, y)
data in a scrollable table below the figure, fetched from the same analysed
array the figure itself was built from — not estimated, not AI-guessed.
Making a curve clickable at all required two things confirmed empirically
first: matplotlib silently drops `set_gid()` from any artist it rasterizes
(exactly the multi-thousand-point series that most need to stay clickable),
and a curve's real bounding box can span most of a panel, so the usual
padded-bbox hit target would swallow clicks meant for things drawn on top of
it. Fixed by decoupling "how a curve renders" from "how it's clicked": a
separate, always-vector, decimated hit-path with a fully transparent but
genuinely clickable stroke (`stroke-opacity:0`, not `stroke:none` — SVG
hit-testing treats those differently). When two curves' hit-paths overlap
almost entirely (panel (a)'s fit line sits right on top of the data it was
fit to — checked, not assumed: 133/133 points within 8px), clicking the same
spot again on an already-selected curve cycles to the next one underneath
instead of reselecting the same thing forever.

**Arrows** — the newest feature. Same gid-tagging and always-vector
hit-target treatment as curves (a transparent hit-path along the body, plus
a small invisible circle at each endpoint). Dragging the body does a rigid
translate of both endpoints, with the same kind of instant preview everything
else gets. Dragging either endpoint — only once that arrow is the sole
selection, so handles don't hijack a click meant to select the arrow in the
first place — resizes and rotates it by moving just that one point;
deliberately *no* faked preview here, since an honest new angle and length
means recomputing the arrowhead geometry, not just applying a transform.
Building this surfaced a real latent bug: two arrow ids (`a_R`, `b_R`)
collided with pre-existing text label ids of the same name, harmless until
arrows got gid-tagged and it became a genuine duplicate-SVG-id defect — fixed
by giving every arrow id an explicit `_arrow` suffix.

## Verification discipline, throughout

Every feature above was checked against the real thing, not assumed:
render-twice-and-diff-the-geometry for anything claiming "no layout impact,"
actual SVG output inspected for hit-testing semantics, a stubbed-DOM harness
run against the *live* local server (not mocked data) for every selection/
resolve/peer-group/bulk-edit path, and a rendered PNG read back for anything
visual. There are now 17 dedicated test files covering selection, axes,
ticks, fonts, editing UX, legends, curves, click-cycling, opacity, and arrows.

## Not yet built

No AI, no chat, no git-commit-per-change, no multi-user editing, no Vercel
deploy. The data table is read-only — no adding or deleting a curve, no
hand-editing a value in the table. Photo-to-data-points and CSV-upload-picks-
a-chart are explicitly out of scope for this phase; the plan is local-first,
one figure at a time, before any of that.
