"""FigForge's assistant: any data files + a plain-language idea -> a figure.

The user's files go into a Python sandbox on Anthropic's machines (Claude's
code execution tool) -- never run on FigForge's own server. There Claude
reads whatever they are (CSV, instrument exports, Excel, a zip of spectra,
notes and macros), does the arithmetic the figure needs, and writes one tidy
CSV table to its output folder. FigForge downloads that table and Claude
builds the figure from it with create_figure, looks at a picture of the
result (view_figure) and tidies it with ops.py's editing tools. The figure
is an ordinary FigForge figure: draggable, editable, exportable.

The loop runs ONE Claude call per step() -- the server calls step() once per
HTTP request, so each request stays short and the page can show progress.
The conversation lives in the project folder (assistant.json) between steps.

The key comes from ANTHROPIC_API_KEY: a Vercel env var when hosted, the
git-ignored .env locally (serve.py loads just that one line).

    python -m figforge.assistant      # one tiny request: is the key working?
"""

import base64
import io
import json
import os
import re
import tempfile
import time
import zipfile

import anthropic

from figforge import csvimport, ops, render

# FIGFORGE_AI_MODEL switches the model (e.g. claude-sonnet-5 for a test);
# a session keeps the model it started with.
MODEL = os.environ.get("FIGFORGE_AI_MODEL", "").strip() or "claude-opus-5"

# List prices, $ per million tokens (input, output). Sandbox time is billed
# separately by Anthropic and isn't included in the cost shown.
PRICES = {"claude-opus-5": (5.00, 25.00), "claude-sonnet-5": (2.00, 10.00),
          "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}
CACHE_WRITE, CACHE_READ = 1.25, 0.10     # x the input price

MAX_STEPS = 20          # Claude calls per figure
MAX_COST_USD = 2.00     # per figure; the session stops (and says so) past this
MAX_VIEWS = 4           # pictures of the figure Claude may ask for
VIEW_DPI = 70           # an 8 x 5 in figure -> 560 x 350 px: plenty to judge layout
MAX_TABLE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGES = 3
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
TABLE_EXTS = (".csv", ".tsv", ".txt", ".dat")

BETAS = ["server-side-fallback-2026-07-01"]
# Models that take an effort change part-way through a conversation (a
# system message) without losing the cached history.
EFFORT_BETA = "mid-conversation-output-config-2026-07-01"
PER_MESSAGE_EFFORT = {"claude-opus-5"}
FIRST_EFFORT, TIDY_EFFORT = "medium", "low"   # making the data + figure; tidying it up
CODE_EXECUTION = {"type": "code_execution_20260521", "name": "code_execution"}

SYSTEM = """\
You make clear, publication-quality scientific figures in FigForge from the \
user's data files and their description of what they want. FigForge draws \
figures with matplotlib and the user can drag and edit everything afterwards.

You have two kinds of tools:
- code_execution: a Python sandbox (numpy, pandas, scipy, openpyxl...). The \
user's files are in $INPUT_DIR (unzip any archive there). Use it to read the \
files, whatever format they are, and do the arithmetic the figure needs \
(unit conversions, normalisation, calibration, smoothing, fits, picking the \
right files). Anything you save in $OUTPUT_DIR is sent to FigForge.
- FigForge tools: create_figure, view_figure, describe_figure and the editing \
tools. FigForge can only plot tables it has received.

How to work:
1. Look at the files and the idea. Read notes, macros or scripts among the \
files - they often name the samples, calibrations and settings to use.
2. In the sandbox, build ONE tidy CSV table with a header row: one column for \
the x values and one column per curve (e.g. channel, energy, 1A_middle, 1B). \
Crop to the range that matters and keep it under ~20,000 rows. Save it as \
$OUTPUT_DIR/<short_name>.csv. If the user gave a tidy table already, it is \
listed as available and you can use it directly.
3. Call create_figure with that table's file name and column names. Data and \
a fit or model usually belong in the same panel: give the fit its own \
x_column (a layer or a second table column) - and if you think of a curve \
later, add_curves adds it to the existing figure.
4. After create_figure, and after every turn that changes the figure, FigForge sends you a picture of it - no need to call view_figure (only for an extra look). Fix what a careful scientist would: axis labels \
with units (matplotlib mathtext works, e.g. $T_1$ ($\\mu$s)), sensible limits, \
line styles that stay distinguishable in black and white, a legend when \
there is more than one curve, readable sizes, nothing overlapping. Use \
add_label for annotations such as element edges or peak names, and \
set_second_axis for a second scale on the top or right (e.g. energy from a \
channel calibration). describe_figure gives the ids the editing tools need.
5. Work in few turns: look at the picture, then make ALL the edits you have in mind in one turn (several tool calls at once - every restyle, label and axis change together); the next picture shows the result. One more round of fixes if needed, then stop. You have about 15 turns in total.
6. Finish with two or three short sentences in plain, everyday words for \
someone who isn't an expert: what the figure shows, and anything you \
couldn't do or had to assume.

FigForge can draw, in 1-9 panels side by side or in a grid (rows):
- flat panels: lines, points and step lines; error bars; shaded bands; bars; \
histograms (hist: FigForge bins the raw values); scatter dots coloured and \
sized by columns; heatmaps, contour lines and filled contours from tidy x, y, \
z columns; hexagon maps (hexbin: how many points fall in each cell, or a \
column's mean there); arrow fields (quiver: x, y and the arrows' x and y \
parts); box plots and violin plots (one column per group). With linear or log \
axes, a second top/right scale (set_second_axis), a second y-axis on the \
right with its own data (set_curve_axis), shaded x or y ranges (add_span).
- 3-D panels (projection "3d"): surfaces (from tidy x, y, z rows - rows \
grouped by x with the same number of rows per x make a mesh that can follow \
a curved edge, e.g. each temperature running up to its own limit field), \
wireframes, surfaces from scattered points (trisurf), 3-D lines and 3-D dots, \
each axis linear or log, turned with set_view. Use 3-D when the idea asks \
for it or a quantity truly depends on two others; one surface is coloured by \
height with a colour bar, several get one colour each and a legend.
- polar panels (projection "polar"): x is the angle in RADIANS (convert \
degrees in the sandbox), y the distance from the middle; lines, dots, bars.
Everywhere: labels, legend, colour bars, fonts, grid lines and minor ticks \
(set_grid), scientific tick numbers and tick spacing (set_tick_format), panel \
grids (set_layout); style_colormap changes a map's colours, range, log scale \
and colour bar. For a fit, compute the fitted curve in the sandbox and plot \
it as its own column.

When the numbers come from a model or formula rather than measurements:
- Use the established model for each case (e.g. a different material gets \
its own law and constants, not one formula for all), and check that its \
pieces agree with each other.
- Check the model in the sandbox against a few well-known reference values \
before drawing it (print them), and say in your reply how close it is \
(e.g. "within about 2x") and that the constants are typical values from \
the literature, not fitted to the user's data.
- Look for places where the formula blows up or goes negative (dividing by \
a value near zero, a power of a negative number) and cap or cut them in a \
physically sensible way rather than plotting a spike.
- Compute each curve or surface only over the range where it is valid, \
right up to its own edge (e.g. up to each material's own limit), not over \
one shared rectangle filled with zeros or blanks.
- Use log axes when values span several powers of ten.
- In the reply, name the main things the model leaves out that matter for \
how it would be used (e.g. direction dependence, strain, sample quality).

Rules: never invent data, values or units that aren't in the files or the \
idea. With no files, the data comes from the idea itself: numbers the user \
typed, a formula or model to evaluate, a simulation, or well-known reference \
values - say which in your reply, and never present computed or example \
numbers as measurements. Curves that can only come from software you can't run (e.g. a RUMP or \
SIMNRA simulation that isn't in the files) must be left out and mentioned. \
Don't ask the user questions - make sensible choices and say what you chose.\
"""

_TEXT = {"type": "string"}
_SCALE = {"type": "string", "enum": list(csvimport.SCALES)}
_LAYER = ops._obj({
    "kind": {"type": "string", "enum": list(csvimport.LAYER_KINDS)},
    "y_column": {"type": "string", "description": "y values; for heatmap/contour/surface the y coordinate of "
                                                    "each cell; hist: the raw values to bin. \"\" for box, violin."},
    "second_column": {"type": "string",
                      "description": "errorbar: the +/- error. band: the upper edge. heatmap/contour/contourf: "
                                     "the value (z) of each cell. scatter, scatter3d: the column that colours "
                                     "the dots (\"\" = one colour). hexbin: a value column averaged per cell "
                                     "(\"\" = count the points). quiver: the arrows' x part. Others: \"\"."},
    "z_column": {"type": "string",
                 "description": "3-D layers (surface, wireframe, trisurf, line3d, scatter3d): the z (height) "
                                "column. quiver: the arrows' y part. Others: \"\"."},
    "size_column": {"type": "string", "description": "scatter only: the column that sizes the dots; \"\" = one size."},
    "columns": {"type": "array", "items": {"type": "string"},
                "description": "box, violin: one column per group; [] otherwise."},
    "x_column": {"type": "string",
                 "description": "This layer's own x column, e.g. a fit on a finer grid; \"\" = the panel's x."},
    "label": {"type": "string", "description": "Legend entry or colour-bar label; \"\" uses the column name."},
})
_LAYERS_DESC = ("Other kinds of curves ([] for none): errorbar, band (shaded between two columns), bar "
                "(bar chart from bin centres + heights), hist (histogram of one column's raw values), "
                "scatter (dots coloured and/or sized by columns, with a colour bar), heatmap / contour / "
                "contourf (a 2-D map from TIDY x, y, z columns - one row per cell; FigForge arranges the "
                "grid), hexbin (hexagon map of x, y points), quiver (arrows at x, y), box / violin (one "
                "column per group; the panel's x_column may be \"\" for these and hist). Only in a "
                "projection \"3d\" panel (and only these there): surface, wireframe (tidy x, y, z "
                "rows), trisurf (scattered x, y, z points), line3d, scatter3d.")

CREATE_FIGURE = {
    "name": "create_figure",
    "description": "Create the figure from a table FigForge has received: 1-9 panels. "
                   "Call exactly once. Refer to columns by their header names. "
                   "Write every title and axis label yourself (matplotlib mathtext works); "
                   "\"\" leaves it empty.",
    "input_schema": ops._obj({
        "table": {"type": "string", "description": "File name of a table FigForge has, e.g. \"spectra.csv\"."},
        "figure_title": {"type": "string", "description": "Title over the whole figure, or \"\"."},
        "rows": {"type": "integer", "description": "Rows of panels: 1 = side by side; e.g. 2 for a 2 x 2 grid of 4."},
        "panels": {"type": "array", "description": "1-9 panels, left to right, then down.",
                   "items": ops._obj({
                       "x_column": {"type": "string", "description": "Header name of the x column (\"\" only for a panel of box plots)."},
                       "y_columns": {"type": "array", "items": {"type": "string"},
                                     "description": "Header names to plot against x, one curve each."},
                       "plot_as": {"type": "string", "enum": list(csvimport.PLOT_KINDS),
                                   "description": "How the y_columns are drawn."},
                       "layers": {"type": "array", "description": _LAYERS_DESC,
                                  "items": _LAYER},
                       "title": _TEXT,
                       "x_label": _TEXT,
                       "y_label": _TEXT,
                       "right_y_columns": {"type": "array", "items": {"type": "string"},
                                           "description": "Columns drawn (as plot_as) against a second "
                                                          "y-axis on the right with its own range - a "
                                                          "second quantity in other units. [] for none."},
                       "right_y_label": {"type": "string", "description": "Label of the right y-axis, or \"\"."},
                       "x_scale": _SCALE,
                       "y_scale": _SCALE,
                       "projection": {"type": "string", "enum": ["flat", "3d", "polar"],
                                      "description": "flat: an ordinary plot. 3d: x, y, z axes (y_columns [] - "
                                                     "every curve is a 3-D layer). polar: x is the angle in "
                                                     "radians, y the distance from the middle."},
                       "z_label": {"type": "string", "description": "3-D panels: the z axis label; \"\" otherwise."},
                       "z_scale": _SCALE,
                   })},
    }),
}

ADD_CURVES = {
    "name": "add_curves",
    "description": "Add more curves to a panel of the existing figure - a fit, a model, a "
                   "band or bars - keeping every edit so far. The table may be the one the "
                   "figure was made from (re-saved with extra columns is fine) or another "
                   "table, whose new columns are merged in. Columns by header name.",
    "input_schema": ops._obj({
        "table": {"type": "string"},
        "panel_id": {"type": "string", "description": "e.g. \"a\"."},
        "x_column": {"type": "string"},
        "y_columns": {"type": "array", "items": {"type": "string"},
                      "description": "Columns drawn as plot_as ([] for none)."},
        "plot_as": {"type": "string", "enum": list(csvimport.PLOT_KINDS)},
        "layers": {"type": "array", "description": _LAYERS_DESC, "items": _LAYER},
    }),
}

VIEW_FIGURE = {
    "name": "view_figure",
    "description": "See a picture of the figure exactly as it looks now. FigForge already "
                   "sends one after every change; use this only for an extra look.",
    "input_schema": ops._obj({}),
}

# Strict mode compiles every strict schema into one grammar, and 14 strict
# tools are over its size limit. ops.py re-checks every argument itself
# (errors go back to Claude as is_error results), so only create_figure --
# the most intricate schema -- stays strict.
TOOLS = [CODE_EXECUTION, CREATE_FIGURE, ADD_CURVES, VIEW_FIGURE] + [
    {k: v for k, v in t.items() if k != "strict"} for t in ops.TOOLS]

# What the page shows while Claude works, per tool.
PROGRESS = {
    "create_figure": "Drew the figure",
    "add_curves": "Added curves",
    "view_figure": "Looked at the figure",
    "describe_figure": "Read the figure's parts",
    "set_text": "Changed some words",
    "style_text": "Changed a text size or color",
    "move_element": "Moved something",
    "add_label": "Added a label",
    "delete_element": "Removed something",
    "set_axis": "Adjusted an axis",
    "style_series": "Restyled a line",
    "set_legend": "Adjusted the legend",
    "set_figure_size": "Resized the figure",
    "set_second_axis": "Added a second axis scale",
    "set_font": "Changed the font",
    "style_colormap": "Adjusted the colours",
    "set_grid": "Changed the grid lines",
    "set_tick_format": "Changed the axis numbers",
    "add_span": "Shaded a region",
    "set_layout": "Arranged the panels",
    "set_curve_axis": "Moved a curve to the other y-axis",
    "set_view": "Turned the 3-D view",
}


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    # A step that runs sandbox code can take a while; the page retries a step
    # that times out (nothing is saved until a step succeeds).
    return anthropic.Anthropic(timeout=240.0, max_retries=1)


def _cost(usage, model=MODEL):
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5"])
    cached_in = (getattr(usage, "cache_creation_input_tokens", 0) or 0) * CACHE_WRITE \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * CACHE_READ
    return ((usage.input_tokens + cached_in) * price_in + usage.output_tokens * price_out) / 1e6


def _usage_row(state, usage, cost, effort, blocks, seconds):
    """One step's tokens and cost, kept in state["usage"] so costs can be measured."""
    return {"step": state["steps"], "effort": effort, "seconds": round(seconds, 1),
            "input": usage.input_tokens,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "output": usage.output_tokens, "cost_usd": round(cost, 5),
            "tools": [b.get("name") for b in blocks if b.get("type") in ("tool_use", "server_tool_use")]}


def _safe_name(name):
    """A file name that's safe as a project path part: basename, plain chars."""
    base = os.path.basename(str(name).replace("\\", "/")) or "table.csv"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "table.csv"


def _kb(n):
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


# ------------------------------------------------------------------ session

def start(fig, files, skipped, idea):
    """A new session. files: [{"name", "data": bytes}] -- they go to Claude's
    sandbox (as one zip when there are several); a file that is already a
    tidy table is also kept as a FigForge table. skipped: [{"name", "size",
    "reason"}] the browser left out. Returns the state to save."""
    tables, notes = {}, []
    for f in files:
        name = f["name"]
        if name.lower().endswith((".csv", ".tsv")) and len(f["data"]) <= MAX_TABLE_BYTES:
            try:
                text = f["data"].decode("utf-8-sig")
                info = csvimport.inspect(text)
            except (UnicodeDecodeError, csvimport.CSVError):
                continue
            safe = _safe_name(name)
            fig.save_table(safe, text)
            tables[safe] = {"from": name}
            cols = ", ".join(f"{c['name']!r}" for c in info["columns"])
            notes.append(f"- {safe} ({info['rows']} rows): columns {cols}")

    uploaded = None
    if len(files) == 1:
        upload_name, payload = _safe_name(files[0]["name"]), files[0]["data"]
    elif files:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.writestr(f["name"], f["data"])
        upload_name, payload = "inputs.zip", buf.getvalue()
    if files:
        uploaded = _client().files.upload(file=(upload_name, io.BytesIO(payload)))

    listing = "\n".join(f"- {f['name']} ({_kb(len(f['data']))})" for f in files)
    left_out = "\n".join(f"- {s['name']} ({_kb(s.get('size', 0))}): {s.get('reason', 'left out')}"
                         for s in skipped[:60])
    if files:
        text = (f"The user's files are in the sandbox as {upload_name}"
                f"{' (a zip of the files below)' if upload_name == 'inputs.zip' else ''}:\n{listing}\n")
    else:
        text = ("The user gave no files. Make the data yourself in the sandbox from their "
                "description - numbers they typed, a formula or model to evaluate, a simulation, "
                "or well-known reference values (say in your reply where the numbers came from; "
                "never pass made-up numbers off as measurements) - and save it as a tidy CSV to "
                "$OUTPUT_DIR as usual.\n")
    if left_out:
        text += f"\nLeft out before upload (not available):\n{left_out}\n"
    if notes:
        text += "\nAlready available to FigForge as tables:\n" + "\n".join(notes) + "\n"
    content = [{"type": "text", "text": text}]

    images = [f for f in files if os.path.splitext(f["name"].lower())[1] in IMAGE_TYPES
              and len(f["data"]) <= MAX_IMAGE_BYTES][:MAX_IMAGES]
    for img in images:
        content.append({"type": "text", "text": f"Image from the user's files: {img['name']}"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": IMAGE_TYPES[os.path.splitext(img["name"].lower())[1]],
            "data": base64.standard_b64encode(img["data"]).decode("ascii")}})
    if uploaded:
        content.append({"type": "container_upload", "file_id": uploaded.id})
    content.append({"type": "text", "text": f"What the user wants:\n{idea.strip()}"})

    return {"version": 2, "model": MODEL, "idea": idea.strip(),
            "messages": [{"role": "user", "content": content}],
            "container": None, "anthropic_files": [uploaded.id] if uploaded else [], "tables": tables,
            "steps": 0, "views": 0, "cost_usd": 0.0,
            "created": False, "done": False, "reply": "", "log": []}


def start_edit(fig, request):
    """A session that changes an existing figure. The figure's data table
    (if it has one) goes to the sandbox, for fits or extra columns."""
    spec = fig.load_spec()
    arrays = fig.load_arrays()
    tables, content = {}, []
    text = fig.load_source_csv()
    upload = None
    if text:
        fig.save_table("figure_data.csv", text)
        tables["figure_data.csv"] = {"from": "the figure's own data"}
        upload = _client().files.upload(file=("figure_data.csv", io.BytesIO(text.encode("utf-8"))))
    content.append({"type": "text", "text":
        "The figure already exists; change it as asked - don't recreate it (create_figure is "
        "off). Here is what it holds now:\n" + json.dumps(ops.describe(spec, arrays)) +
        ("\n\nIts data table is in the sandbox and available to FigForge as figure_data.csv "
         "(use add_curves with it, or save a new table with extra columns)." if text else
         "\n\nThis figure has no data table; restyle and annotate it with the editing tools.")})
    if upload:
        content.append({"type": "container_upload", "file_id": upload.id})
    content.append({"type": "text", "text": f"What the user wants changed:\n{request.strip()}"})
    return {"version": 2, "model": MODEL, "idea": request.strip(), "mode": "edit",
            "messages": [{"role": "user", "content": content}],
            "container": None, "anthropic_files": [upload.id] if upload else [], "tables": tables,
            "steps": 0, "views": 0, "cost_usd": 0.0,
            "created": True, "done": False, "reply": "", "log": []}


def step(state, fig):
    """One Claude call and the tools it asks for. Mutates and returns state.
    -> (state, spec or None if unchanged, events). Raises anthropic errors
    (the caller doesn't save anything then, so the step can be retried)."""
    if state["done"]:
        return state, None, []

    kw = {"container": state["container"]} if state.get("container") else {}
    # The last allowed turn is a wrap-up: no tools, just the summary.
    wrap_up = state["steps"] >= MAX_STEPS - 1 or state["cost_usd"] >= MAX_COST_USD * 0.9
    if wrap_up:
        kw["tool_choice"] = {"type": "none"}
    model = state.get("model") or MODEL
    # The request's own effort never changes (that would drop the cache);
    # tidy-up rounds lower it with a system message instead (see below).
    effort = state.get("effort") or FIRST_EFFORT
    t0 = time.time()
    response = _client().beta.messages.create(
        model=model,
        max_tokens=16000,
        # Tools + system are the same for every figure: their own cache
        # breakpoint lets figures started within minutes share them.
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=TOOLS,
        messages=state["messages"],
        output_config={"effort": FIRST_EFFORT},
        cache_control={"type": "ephemeral"},   # tools + system + history: a cached prefix
        betas=BETAS + ([EFFORT_BETA] if model in PER_MESSAGE_EFFORT else []),
        fallbacks="default",
        **kw,
    )
    state["steps"] += 1
    cost = _cost(response.usage, model)
    state["cost_usd"] = round(state["cost_usd"] + cost, 5)
    if getattr(response, "container", None):
        state["container"] = response.container.id
    blocks = [b.to_dict() for b in response.content]
    state["messages"].append({"role": "assistant", "content": blocks})
    state.setdefault("usage", []).append(
        _usage_row(state, response.usage, cost, effort, blocks, time.time() - t0))

    events = []
    if any(b["type"] == "server_tool_use" for b in blocks):
        events.append("Worked with your data in the sandbox")
    events += _collect_tables(state, fig, blocks)

    if response.stop_reason == "refusal":
        return _finish(state, "Claude declined to make this figure. Try describing it differently."), None, events
    if response.stop_reason == "pause_turn":
        # A long sandbox run paused; the next step continues it as-is.
        return _check_limits(state), None, events

    calls = [b for b in response.content if b.type == "tool_use"]
    if not calls:
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text = text or "Claude ran out of room before finishing."
        return _finish(state, text or "Done."), None, events

    spec = fig.load_spec() if state["created"] else None
    arrays = fig.load_arrays() if state["created"] else None
    changed, results = False, []
    for call in calls:
        try:
            content, spec, arrays, did_change = _run_tool(state, fig, call, spec, arrays)
            changed |= did_change
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
            events.append(PROGRESS.get(call.name, call.name))
        except _ToolError as e:
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": str(e), "is_error": True})
    # Claude sees the figure after every change without spending a step on
    # view_figure (each picture still counts toward MAX_VIEWS).
    if changed and not any(c.name == "view_figure" for c in calls) and state["views"] < MAX_VIEWS:
        state["views"] += 1
        results += [{"type": "text", "text": "The figure as it looks after these changes:"},
                    _picture(spec, arrays)]
        events.append(PROGRESS["view_figure"])
    # Once the figure exists and has been changed, the rest is tidying up:
    # cheaper, shorter thinking from here on.
    if changed and effort != TIDY_EFFORT and model in PER_MESSAGE_EFFORT:
        state["effort"] = TIDY_EFFORT
        state["messages"].append({"role": "system", "content": [],
                                  "output_config": {"effort": TIDY_EFFORT}})
    state["messages"].append({"role": "user", "content": results})
    state["log"] += events
    return _check_limits(state), (spec if changed else None), events


def _check_limits(state):
    # Normally the wrap-up turn (tool_choice none) ends the session with a
    # summary; this only catches a turn that still asked for tools.
    if not state["done"] and (state["steps"] >= MAX_STEPS or state["cost_usd"] >= MAX_COST_USD):
        note = ("I stopped here to keep this figure's cost down." if state["cost_usd"] >= MAX_COST_USD
                else "I stopped after the maximum number of steps.")
        _finish(state, note + " You can keep adjusting it by hand.")
    return state


def _finish(state, reply):
    state["done"], state["reply"] = True, reply
    return state


def cleanup(state):
    """Delete this session's files from Anthropic's file storage (best effort)."""
    client = _client()
    for fid in state.get("anthropic_files", []):
        try:
            client.files.delete(fid)
        except anthropic.APIError:
            pass
    state["anthropic_files"] = []


def _collect_tables(state, fig, blocks):
    """Download the tables Claude saved to $OUTPUT_DIR in this response."""
    events = []
    client = None
    for b in blocks:
        if b.get("type") != "bash_code_execution_tool_result":
            continue
        result = b.get("content") or {}
        for ref in result.get("content") or []:
            fid = ref.get("file_id")
            if not fid:
                continue
            client = client or _client()
            meta = client.files.retrieve_metadata(fid)
            state.setdefault("anthropic_files", []).append(fid)
            name = _safe_name(meta.filename)
            if not name.lower().endswith(TABLE_EXTS) or (meta.size_bytes or 0) > MAX_TABLE_BYTES:
                continue
            data = client.files.download(fid).read()
            try:
                fig.save_table(name, data.decode("utf-8-sig"))
            except UnicodeDecodeError:
                continue
            state["tables"][name] = {"file_id": fid}
            events.append(f"Made a table: {name}")
    return events


class _ToolError(Exception):
    pass


def _column(names, want, table):
    """Column index for a header name: exact, then ignoring case/spaces."""
    if want in names:
        if names.count(want) > 1:
            raise _ToolError(f"{table} has more than one column named {want!r}")
        return names.index(want)
    norm = lambda s: re.sub(r"\s+", "", str(s)).lower()
    hits = [i for i, n in enumerate(names) if norm(n) == norm(want)]
    if len(hits) == 1:
        return hits[0]
    raise _ToolError(f"{table} has no column {want!r}. Its columns: {names}")


def _layers(names, table, layers):
    out = []
    for ly in layers or []:
        kind = ly.get("kind")
        item = {"kind": kind, "label": ly.get("label") or ""}
        if kind in csvimport.GROUP_KINDS:
            item["columns"] = [_column(names, c, table) for c in ly.get("columns") or []]
        else:
            item["y"] = _column(names, ly.get("y_column") or "", table)
            if ly.get("second_column"):
                item["extra"] = _column(names, ly["second_column"], table)
            if ly.get("size_column"):
                item["size"] = _column(names, ly["size_column"], table)
            if ly.get("z_column"):
                item["z"] = _column(names, ly["z_column"], table)
        if ly.get("x_column"):
            item["x"] = _column(names, ly["x_column"], table)
        out.append(item)
    return out


def _add_curves(state, fig, args, spec):
    table = _safe_name(args.get("table") or "")
    if table not in state["tables"]:
        raise _ToolError(f"FigForge has no table {table!r} (it has: {', '.join(state['tables'])}).")
    for key in ("panel_id", "x_column", "y_columns", "plot_as", "layers"):
        if key not in args:
            raise _ToolError(f"add_curves needs {key}")
    text, base = fig.load_table(table), fig.load_source_csv() or ""
    try:
        have = set(csvimport.parse(base)[0]) if base else set()
        names = csvimport.parse(text)[0]
        merged_note = ""
        if base and not have <= set(names):
            text, added = csvimport.merge_tables(base, text)
            names = csvimport.parse(text)[0]
            merged_note = f" (merged {len(added)} new column(s) of {table} into the figure's table)"
        batch = {"x": _column(names, args["x_column"], table) if args.get("x_column") else "",
                 "ys": [_column(names, y, table) for y in args["y_columns"]],
                 "kind": args["plot_as"], "layers": _layers(names, table, args["layers"])}
        new_spec, new_arrays, ids = csvimport.add_batch(spec, fig.name, text, args["panel_id"], batch)
        render.render(new_spec, new_arrays)
    except csvimport.CSVError as e:
        raise _ToolError(f"Couldn't add those curves: {e}")
    except _ToolError:
        raise
    except Exception as e:
        raise _ToolError(f"Those curves couldn't be drawn ({type(e).__name__}: {e}).")
    fig.save_arrays(new_arrays)
    fig.save_source_csv(text)
    p = next(q for q in new_spec["panels"] if q["id"] == args["panel_id"])
    return (f"Added {', '.join(ids)}{merged_note}. Panel limits stay x {p['xlim']}, y {p['ylim']} - "
            "use set_axis if the new curves need more room."), new_spec, new_arrays


def _picture(spec, arrays):
    """An image block of the figure as it is drawn now."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "view.png")
        render.render_png(spec, arrays, path, dpi=VIEW_DPI)
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _run_tool(state, fig, call, spec, arrays):
    """-> (tool_result content, spec, arrays, changed)."""
    args = call.input or {}
    if call.name == "create_figure":
        if state["created"]:
            raise _ToolError("The figure already exists - change it with the editing tools.")
        table = _safe_name(args.get("table") or "")
        if table not in state["tables"]:
            have = ", ".join(state["tables"]) or "none yet"
            raise _ToolError(f"FigForge has no table {table!r} (it has: {have}). Save a CSV to "
                             "$OUTPUT_DIR in the sandbox first; it arrives when that code finishes.")
        text = fig.load_table(table)
        try:
            names = csvimport.parse(text)[0]
            panels = [{"x": _column(names, p["x_column"], table) if p.get("x_column") else "",
                       "ys": [_column(names, y, table) for y in p["y_columns"]],
                       "kind": p["plot_as"], "title": p["title"], "xlabel": p["x_label"],
                       "ylabel": p["y_label"], "xscale": p["x_scale"], "yscale": p["y_scale"],
                       "layers": _layers(names, table, p.get("layers")),
                       "right_ys": [_column(names, y, table) for y in p.get("right_y_columns") or []],
                       "right_label": p.get("right_y_label") or None,
                       "projection": p.get("projection") or "flat",
                       "zlabel": p.get("z_label") or None, "zscale": p.get("z_scale") or "linear"}
                      for p in args.get("panels") or []]
            new_spec, new_arrays = csvimport.build_panels(
                fig.name, text, panels, table, args.get("figure_title") or "", args.get("rows") or 1)
            render.render(new_spec, new_arrays)   # prove it draws before keeping it
        except csvimport.CSVError as e:
            raise _ToolError(f"Couldn't make that figure: {e}")
        except _ToolError:
            raise
        except Exception as e:
            raise _ToolError(f"That figure couldn't be drawn ({type(e).__name__}: {e}).")
        fig.save_arrays(new_arrays)
        fig.save_source_csv(text)            # what Start over rebuilds from
        state["created"] = True
        summary = json.dumps(ops.describe(new_spec, new_arrays))
        return f"Created. The figure now:\n{summary}", new_spec, new_arrays, True

    if not state["created"]:
        raise _ToolError("There is no figure yet - call create_figure first.")

    if call.name == "add_curves":
        content, new_spec, new_arrays = _add_curves(state, fig, args, spec)
        return content, new_spec, new_arrays, True

    if call.name == "view_figure":
        if state["views"] >= MAX_VIEWS:
            raise _ToolError("No more pictures this session - finish up with what you know.")
        state["views"] += 1
        return [_picture(spec, arrays), {"type": "text", "text": "The figure as it looks now."}],             spec, arrays, False

    new_spec, results, changed = ops.apply(spec, [{"name": call.name, "input": args}], arrays)
    result = results[0]
    if result["is_error"]:
        raise _ToolError(result["content"])
    if changed:
        try:
            render.render(new_spec, arrays)
        except Exception as e:
            raise _ToolError(f"That change broke the drawing ({type(e).__name__}: {e}); "
                             "it was not applied.")
    return result["content"], new_spec, arrays, changed


# ------------------------------------------------------ how this was made

HOWTO_SYSTEM = """\
You explain how a FigForge figure was made by an AI, for someone curious \
who is not an expert - write as if to a bright child: short sentences, \
everyday words, no jargon without a two-word explanation. You get the \
user's idea, the commands the AI ran in its Python sandbox (with a bit of \
their output), and the AI's closing note. Only describe what really \
happened in those commands; never invent steps, numbers or sources."""

HOWTO_SCHEMA = {
    "type": "object",
    "properties": {
        "idea": {"type": "string", "description":
                 "The idea as the AI understood it, 1-2 plain sentences."},
        "math": {"type": "array", "description":
                 "The formulas the numbers came from (0-5); empty if the data was only "
                 "read and rearranged.", "items": {
                     "type": "object",
                     "properties": {
                         "name": {"type": "string", "description": "2-5 plain words"},
                         "tex": {"type": "string", "description":
                                 "The formula in matplotlib mathtext (LaTeX subset), "
                                 "without surrounding dollar signs, e.g. A = \\pi r^2"},
                         "plain": {"type": "string", "description":
                                   "The same formula in plain Unicode, e.g. A = π r²"},
                         "why": {"type": "string", "description":
                                 "One plain sentence: what it gives and why it was used"},
                     },
                     "required": ["name", "tex", "plain", "why"],
                     "additionalProperties": False}},
        "code": {"type": "string", "description":
                 "ONE clean, self-contained Python script that redoes what the sandbox "
                 "commands did to make the table, with a short plain-words comment above "
                 "each part. Use numpy and the csv module; scipy only for fits it really needs; no pandas. Read "
                 "the user's files from os.environ.get('INPUT_DIR', 'inputs'); write the "
                 "table to 'ai_table.csv'; end with a small matplotlib plot of the table "
                 "so running it shows a picture. Leave out the exploring (ls, head, cat)."},
        "needs_files": {"type": "boolean", "description":
                        "True if the script reads the user's files."},
        "steps": {"type": "array", "items": {"type": "string"}, "description":
                  "3-7 steps the AI took, each one plain sentence saying what and why."},
        "try_it": {"type": "array", "items": {"type": "string"}, "description":
                   "2-3 small changes to try in the script, each naming the line or "
                   "number to change and what will happen."},
    },
    "required": ["idea", "math", "code", "needs_files", "steps", "try_it"],
    "additionalProperties": False,
}
# Explaining a finished figure is an easy job: Haiku 5.5 does it well for
# about $0.001 (Opus 5: ~$0.07, Sonnet 5.5: ~$0.03; measured 2026-10-12).
HOWTO_MODEL = os.environ.get("FIGFORGE_HOWTO_MODEL", "").strip() or "claude-haiku-5-5"
HOWTO_MAX_CHARS = 24000     # sandbox commands sent to the explainer


def sandbox_commands(state):
    """[{"command", "ok", "output"}] -- what Claude ran in its sandbox, in order."""
    out, by_id = [], {}
    for m in state.get("messages", []):
        if m.get("role") != "assistant":
            continue
        for b in m.get("content") or []:
            if b.get("type") == "server_tool_use":
                cmd = (b.get("input") or {}).get("command") or (b.get("input") or {}).get("code")
                if cmd:
                    by_id[b.get("id")] = len(out)
                    out.append({"command": cmd, "ok": True, "output": ""})
            elif b.get("type") == "bash_code_execution_tool_result" and b.get("tool_use_id") in by_id:
                r = b.get("content") or {}
                row = out[by_id[b["tool_use_id"]]]
                row["ok"] = r.get("return_code", 0) == 0
                row["output"] = ((r.get("stdout") or "") + (r.get("stderr") or ""))[:400]
    return out


def can_explain(state):
    return bool(state and state.get("done") and state.get("created") and state.get("mode") != "edit")


def how_made(state, model=None):
    """One cheap Claude call that explains an AI figure: the idea, the math,
    a clean commented script, the steps and things to try. -> (howto, cost)."""
    commands = sandbox_commands(state)
    parts, used = [], 0
    for i, c in enumerate(commands, 1):
        piece = (f"--- command {i}{'' if c['ok'] else ' (failed; fixed later)'}\n{c['command']}\n"
                 f"output: {c['output'] or '(none)'}\n")
        if used + len(piece) > HOWTO_MAX_CHARS:
            parts.append(f"--- ({len(commands) - i + 1} later commands left out for length)\n")
            break
        parts.append(piece)
        used += len(piece)
    first = state["messages"][0]["content"]
    given = next((b["text"] for b in first if b.get("type") == "text"), "")
    text = (f"The user's idea:\n{state.get('idea', '')}\n\nWhat the AI was given:\n{given}\n\n"
            f"Sandbox commands, in order:\n{''.join(parts) or '(none - the data came from a table the user gave)'}\n"
            f"The AI's closing note:\n{state.get('reply', '')}")
    model = model or HOWTO_MODEL
    # Haiku has no server-side refusal fallback.
    fb = {} if "haiku" in model else {"betas": BETAS, "fallbacks": "default"}
    response = _client().beta.messages.create(
        model=model,
        max_tokens=8000,
        system=HOWTO_SYSTEM,
        messages=[{"role": "user", "content": text}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": HOWTO_SCHEMA}},
        **fb,
    )
    cost = _cost(response.usage, model)
    if response.stop_reason in ("refusal", "max_tokens"):
        raise ValueError("Claude couldn't write the explanation this time.")
    howto = json.loads(next(b.text for b in response.content if b.type == "text"))
    for m in howto["math"]:
        m["svg"] = math_svg(m["tex"])
    howto["commands"] = commands
    howto["cost_usd"] = round(cost, 5)
    howto["model"] = model
    return howto, cost


def math_svg(tex):
    """A formula drawn by matplotlib's mathtext as a small SVG, or None if
    it can't be drawn (the page then shows the plain version)."""
    from matplotlib import mathtext
    from matplotlib.font_manager import FontProperties
    buf = io.BytesIO()
    try:
        mathtext.math_to_image(f"${tex.strip().strip('$')}$", buf, prop=FontProperties(size=15),
                               dpi=72, format="svg")
    except Exception:
        return None
    return buf.getvalue().decode("utf-8")


# ----------------------------------------------------------- connection check

def ping():
    """One minimal request, to prove the key and model work from wherever
    this runs. Returns what came back and what it cost; raises
    anthropic.APIError subclasses on failure (bad key, no credit, ...)."""
    response = anthropic.Anthropic(timeout=50.0, max_retries=1).beta.messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={"effort": "low"},
        # If a safety classifier declines, Anthropic retries on its
        # recommended model instead of returning the refusal.
        betas=BETAS,
        fallbacks="default",
        messages=[{"role": "user",
                   "content": "Reply with one short, cheerful sentence confirming "
                              "FigForge can reach you."}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    u = response.usage
    return {
        "ok": response.stop_reason != "refusal",
        "model": response.model,
        "reply": text,
        "stop_reason": response.stop_reason,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cost_usd": round(_cost(u), 5),
        "request_id": response._request_id,
    }


def load_key_from_dotenv(path=".env"):
    """Just ANTHROPIC_API_KEY from .env (never printed). Only that line: the
    Supabase lines there would switch the local editor to online storage."""
    if os.environ.get("ANTHROPIC_API_KEY") or not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "ANTHROPIC_API_KEY" and value.strip():
            os.environ["ANTHROPIC_API_KEY"] = value.strip()


if __name__ == "__main__":
    load_key_from_dotenv()
    if not configured():
        raise SystemExit("ANTHROPIC_API_KEY is not set -- add it to .env first.")
    r = ping()
    print(f"{r['model']}: {r['reply']}")
    print(f"{r['input_tokens']} in + {r['output_tokens']} out tokens = ${r['cost_usd']:.5f}"
          f"  (request {r['request_id']})")
