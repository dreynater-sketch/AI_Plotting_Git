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

- **Click** any label to select it (or pick it from the Elements list, which is
  how you reach labels hidden under others).
- **Drag** it anywhere. Arrow keys nudge by 1 pt, Shift+arrows by 10.
- **Retype** it, including matplotlib mathtext (`$\beta_1$`, `$Q_L$`), change
  its **size**, **color**, alignment, and whether it has a background box.
- **Ctrl+Z** to undo. **Rebuild** discards all edits and regenerates from the CSV.
- **figure.py** downloads a standalone script that reproduces exactly what you
  see. **PNG** exports at the spec's dpi.

Panel titles and axis labels are editable (text/size/color) but not draggable —
matplotlib positions those itself.

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
