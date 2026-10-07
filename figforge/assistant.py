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
import zipfile

import anthropic

from figforge import csvimport, ops, render

MODEL = "claude-opus-5"

# Claude Opus 5 list prices, $ per million tokens. (Sandbox time is billed
# separately by Anthropic and isn't included in the cost shown.)
PRICE_IN, PRICE_OUT = 5.00, 25.00
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
4. Call view_figure, then fix what a careful scientist would: axis labels \
with units (matplotlib mathtext works, e.g. $T_1$ ($\\mu$s)), sensible limits, \
line styles that stay distinguishable in black and white, a legend when \
there is more than one curve, readable sizes, nothing overlapping. Use \
add_label for annotations such as element edges or peak names, and \
set_second_axis for a second scale on the top or right (e.g. energy from a \
channel calibration). describe_figure gives the ids the editing tools need.
5. Work in few turns: make ALL the edits you have in mind in one turn (several tool calls at once - e.g. every restyle, label and axis change together), then view_figure once to check, then one more round of fixes if needed. You have about 15 turns in total. Stop when it's good.
6. Finish with two or three short sentences in plain, everyday words for \
someone who isn't an expert: what the figure shows, and anything you \
couldn't do or had to assume.

FigForge can draw lines, points and step lines; error bars; shaded bands; \
bars (bar charts, and histograms from bin centres and counts you compute); \
scatter dots coloured and sized by columns; heatmaps, contour lines and \
filled contours from tidy x, y, z columns; box plots - in 1-4 panels side by \
side, with linear or log axes, a second top/right scale, labels, legend, \
colour bars and fonts. style_colormap changes a map's colours, range, log \
scale and colour bar. It cannot yet draw 3D or polar plots - if the idea \
needs one, make the closest honest figure and say so. For a fit, compute \
the fitted curve in the sandbox and plot it as its own column.

Rules: never invent data, values or units that aren't in the files or the \
idea. Curves that can only come from software you can't run (e.g. a RUMP or \
SIMNRA simulation that isn't in the files) must be left out and mentioned. \
Don't ask the user questions - make sensible choices and say what you chose.\
"""

_TEXT = {"type": "string"}
_SCALE = {"type": "string", "enum": list(csvimport.SCALES)}
_LAYER = ops._obj({
    "kind": {"type": "string", "enum": list(csvimport.LAYER_KINDS)},
    "y_column": {"type": "string", "description": "y values; for heatmap/contour the y coordinate of each cell. \"\" for box."},
    "second_column": {"type": "string",
                      "description": "errorbar: the +/- error. band: the upper edge. heatmap/contour/contourf: "
                                     "the value (z) of each cell. scatter: the column that colours the dots "
                                     "(\"\" = one colour). bar, box: \"\"."},
    "size_column": {"type": "string", "description": "scatter only: the column that sizes the dots; \"\" = one size."},
    "columns": {"type": "array", "items": {"type": "string"},
                "description": "box only: one column per group (each box); [] otherwise."},
    "x_column": {"type": "string",
                 "description": "This layer's own x column, e.g. a fit on a finer grid; \"\" = the panel's x."},
    "label": {"type": "string", "description": "Legend entry or colour-bar label; \"\" uses the column name."},
})
_LAYERS_DESC = ("Other kinds of curves ([] for none): errorbar, band (shaded between two columns), bar "
                "(bar chart; a histogram from bin centres + counts), scatter (dots coloured and/or sized "
                "by columns, with a colour bar), heatmap / contour / contourf (a 2-D map from TIDY "
                "x, y, z columns - one row per cell; FigForge arranges the grid), box (box plot, one "
                "column per group; the panel's x_column may be \"\").")

CREATE_FIGURE = {
    "name": "create_figure",
    "description": "Create the figure from a table FigForge has received: one to four panels "
                   "side by side. Call exactly once. Refer to columns by their header names. "
                   "Write every title and axis label yourself (matplotlib mathtext works); "
                   "\"\" leaves it empty.",
    "input_schema": ops._obj({
        "table": {"type": "string", "description": "File name of a table FigForge has, e.g. \"spectra.csv\"."},
        "figure_title": {"type": "string", "description": "Title over the whole figure, or \"\"."},
        "panels": {"type": "array", "description": "1-4 panels, left to right.",
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
                       "x_scale": _SCALE,
                       "y_scale": _SCALE,
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
    "description": "See a picture of the figure exactly as it looks now. Use it after "
                   "create_figure and after visible changes, to check layout and overlaps.",
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
}


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    # A step that runs sandbox code can take a while; the page retries a step
    # that times out (nothing is saved until a step succeeds).
    return anthropic.Anthropic(timeout=240.0, max_retries=1)


def _cost(usage):
    cached_in = (getattr(usage, "cache_creation_input_tokens", 0) or 0) * CACHE_WRITE \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * CACHE_READ
    return ((usage.input_tokens + cached_in) * PRICE_IN + usage.output_tokens * PRICE_OUT) / 1e6


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
    if not files:
        raise ValueError("add at least one file")
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

    if len(files) == 1:
        upload_name, payload = _safe_name(files[0]["name"]), files[0]["data"]
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.writestr(f["name"], f["data"])
        upload_name, payload = "inputs.zip", buf.getvalue()
    uploaded = _client().files.upload(file=(upload_name, io.BytesIO(payload)))

    listing = "\n".join(f"- {f['name']} ({_kb(len(f['data']))})" for f in files)
    left_out = "\n".join(f"- {s['name']} ({_kb(s.get('size', 0))}): {s.get('reason', 'left out')}"
                         for s in skipped[:60])
    text = (f"The user's files are in the sandbox as {upload_name}"
            f"{' (a zip of the files below)' if upload_name == 'inputs.zip' else ''}:\n{listing}\n")
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
    content.append({"type": "container_upload", "file_id": uploaded.id})
    content.append({"type": "text", "text": f"What the user wants:\n{idea.strip()}"})

    return {"version": 2, "model": MODEL, "idea": idea.strip(),
            "messages": [{"role": "user", "content": content}],
            "container": None, "anthropic_files": [uploaded.id], "tables": tables,
            "steps": 0, "views": 0, "cost_usd": 0.0,
            "created": False, "done": False, "reply": "", "log": []}


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
    response = _client().beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        tools=TOOLS,
        messages=state["messages"],
        output_config={"effort": "medium"},
        cache_control={"type": "ephemeral"},   # tools + system + history: a cached prefix
        betas=BETAS,
        fallbacks="default",
        **kw,
    )
    state["steps"] += 1
    state["cost_usd"] = round(state["cost_usd"] + _cost(response.usage), 5)
    if getattr(response, "container", None):
        state["container"] = response.container.id
    blocks = [b.to_dict() for b in response.content]
    state["messages"].append({"role": "assistant", "content": blocks})

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
        if kind == "box":
            item["columns"] = [_column(names, c, table) for c in ly.get("columns") or []]
        else:
            item["y"] = _column(names, ly.get("y_column") or "", table)
            if ly.get("second_column"):
                item["extra"] = _column(names, ly["second_column"], table)
            if ly.get("size_column"):
                item["size"] = _column(names, ly["size_column"], table)
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
                       "layers": _layers(names, table, p.get("layers"))}
                      for p in args.get("panels") or []]
            new_spec, new_arrays = csvimport.build_panels(
                fig.name, text, panels, table, args.get("figure_title") or "")
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
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "view.png")
            render.render_png(spec, arrays, path, dpi=VIEW_DPI)
            data = base64.standard_b64encode(open(path, "rb").read()).decode("ascii")
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                {"type": "text", "text": "The figure as it looks now."}], spec, arrays, False

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
