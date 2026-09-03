# FigForge — MVP

Drag-and-edit publication figures that stay reproducible as matplotlib code.

This is the **Phase 0/1 spike** from `figforge_spec.md`: the risky core proven
end to end, with **no AI and no API keys**. The first figure is the Q-circle
extraction (`qcircle_C_R_Goff.py`) for the 2 K Nb-plated Cu cavity.

## Run it

```bash
python build.py     # CSV -> analysis -> spec.json -> figure.svg + figure.py
python serve.py     # opens http://127.0.0.1:8000
```

Local only — the server binds `127.0.0.1` and talks to nothing else.

## What you can do

- **Click** any label to select it, or **click a panel's plot area** to select
  its axes (for xlim/ylim/scale/aspect — see below). Pick either from the
  Elements list too, which is how you reach labels hidden under others.
- **Ctrl-click** (Cmd on macOS) adds or removes one thing from the selection.
  **Drag a box** over empty canvas space to select everything under it — hold
  Ctrl while dragging to add the box's contents to the current selection
  instead of replacing it. Clicking empty space outside any panel deselects.
- **Drag** a label anywhere. Arrow keys nudge by 1 pt, Shift+arrows by 10.
- **Retype** it, including matplotlib mathtext (`$\beta_1$`, `$Q_L$`), change
  its **size**, **color**, alignment, and whether it has a background box.
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
figforge/
  analyze.py            CSV -> curve arrays + derived scalars (the science)
  spec_builder.py       analysis -> initial SPEC
  render.py             SPEC -> SVG with gids + geometry map
  codegen.py            SPEC -> standalone figure.py
  server.py             local HTTP API
  project.py            figure folder layout
web/                    the editor (vanilla JS, no build step)
figures/qcircle/
  data/vna_sweep.csv    raw VNA measurement
  data/curves.npz       analysed arrays (generated)
  spec.json             the semantic layer — the thing you edit
  figure.svg            last render (generated)
  figure.py             standalone reproducer (generated)
```

`figure.py` imports nothing from FigForge. It needs numpy, matplotlib and
`data/curves.npz` next to it, and that's it.

## Not in this build

No AI, no chat, no git-commit-per-change, no multi-user, no Vercel. Arrows,
legends and axis limits render from the SPEC but aren't interactive yet.
Vercel needs a Python serverless function for the render step — matplotlib has
to live somewhere — so that's a deliberate later step.
