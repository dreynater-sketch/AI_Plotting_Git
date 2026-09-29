"""FigForge's assistant: a CSV file + a plain-language idea -> a figure.

Claude reads a short summary of the file (never the whole thing), designs
the figure with create_figure, looks at a picture of it (view_figure), and
tidies it with ops.py's editing tools. It never runs code: every change goes
through a validated tool, and every result is re-rendered before it's kept.

The loop runs ONE Claude call per step() -- the server calls step() once per
HTTP request, so no request comes near Vercel's 60 s limit, and the page can
show progress between steps. The conversation lives in the project folder
(assistant.json) between steps.

The key comes from ANTHROPIC_API_KEY: a Vercel env var when hosted, the
git-ignored .env locally (serve.py loads just that one line).

    python -m figforge.assistant      # one tiny request: is the key working?
"""

import base64
import json
import os
import tempfile

import anthropic

from figforge import csvimport, ops, render

MODEL = "claude-opus-5"

# Claude Opus 5 list prices, $ per million tokens.
PRICE_IN, PRICE_OUT = 5.00, 25.00
CACHE_WRITE, CACHE_READ = 1.25, 0.10     # x the input price

MAX_STEPS = 14          # Claude calls per figure
MAX_COST_USD = 1.50     # per figure; the session stops (and says so) past this
MAX_VIEWS = 4           # pictures of the figure Claude may ask for
VIEW_DPI = 70           # an 8 x 5 in figure -> 560 x 350 px: plenty to judge layout
SAMPLE_ROWS = 6         # raw lines of the file Claude sees

BETAS = ["server-side-fallback-2026-07-01"]

SYSTEM = """\
You make clear, publication-quality scientific figures in FigForge from a \
user's CSV file and their description of what they want. FigForge draws the \
figure with matplotlib; you can't run code, you work only through the tools.

How to work:
1. Read the data summary and the idea. Decide which columns go on which axes, \
how many panels (1-4, side by side), and whether lines, points, or both suit \
the data: scattered measurements read best as points, dense smooth series as lines.
2. Call create_figure once. Columns are referred to by their "index" in the summary.
3. Call view_figure to see the result, then fix what a careful scientist would: \
clear axis labels with units (matplotlib mathtext works, e.g. $T_1$ ($\\mu$s)), \
sensible limits, a legend only when there is more than one curve, readable \
sizes, nothing overlapping. describe_figure gives the ids the editing tools need.
4. Look again only after a visible change. Stop when it's good - don't polish forever.
5. Finish with two or three short sentences in plain, everyday words for \
someone who isn't an expert: what the figure shows, and anything you couldn't do.

Rules: never invent data, values or units that aren't in the file or the \
idea. If the idea needs something these tools can't do (curve fits, \
histograms, error bars, computed columns, colour maps), make the closest \
honest figure and say what's missing in your final sentences. Don't ask the \
user questions - make sensible choices.\
"""

_TEXT = {"type": "string"}
_SCALE = {"type": "string", "enum": list(csvimport.SCALES)}

CREATE_FIGURE = {
    "name": "create_figure",
    "description": "Create the figure from the CSV file: one to four panels side by side. "
                   "Call exactly once, before any other editing tool. Write every title and "
                   "axis label yourself (matplotlib mathtext works); \"\" leaves it empty.",
    "input_schema": ops._obj({
        "figure_title": {"type": "string", "description": "Title over the whole figure, or \"\"."},
        "panels": {"type": "array", "description": "1-4 panels, left to right.",
                   "items": ops._obj({
                       "x_column": {"type": "integer", "description": "Column index for the x axis."},
                       "y_columns": {"type": "array", "items": {"type": "integer"},
                                     "description": "Column indices to plot against x, one curve each."},
                       "plot_as": {"type": "string", "enum": list(csvimport.PLOT_KINDS)},
                       "title": _TEXT,
                       "x_label": _TEXT,
                       "y_label": _TEXT,
                       "x_scale": _SCALE,
                       "y_scale": _SCALE,
                   })},
    }),
    "strict": True,
}

VIEW_FIGURE = {
    "name": "view_figure",
    "description": "See a picture of the figure exactly as it looks now. Use it after "
                   "create_figure and after visible changes, to check layout and overlaps.",
    "input_schema": ops._obj({}),
    "strict": True,
}

TOOLS = [CREATE_FIGURE, VIEW_FIGURE] + ops.TOOLS

# What the page shows while Claude works, per tool.
PROGRESS = {
    "create_figure": "Drew the figure",
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
}


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    # Each step must finish well inside Vercel's 60 s; a timed-out step is
    # simply retried by the page (nothing is saved until a step succeeds).
    return anthropic.Anthropic(timeout=50.0, max_retries=1)


def _cost(usage):
    cached_in = (getattr(usage, "cache_creation_input_tokens", 0) or 0) * CACHE_WRITE \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * CACHE_READ
    return ((usage.input_tokens + cached_in) * PRICE_IN + usage.output_tokens * PRICE_OUT) / 1e6


# ------------------------------------------------------------------ session

def data_summary(csv_text, filename):
    """What Claude sees of the file: its columns and a few raw lines."""
    info = csvimport.inspect(csv_text)
    sample = [l[:300] for l in csv_text.splitlines()
              if l.strip() and not l.lstrip().startswith("#")][:SAMPLE_ROWS + 1]
    return (f"File: {filename or '(unnamed)'} - {info['rows']} data rows, "
            f"{info['separator']}-separated.\n"
            f"Columns:\n{json.dumps(info['columns'], indent=1)}\n"
            f"First lines of the file:\n" + "\n".join(sample))


def start(csv_text, filename, idea):
    """A new session for one figure. Raises csvimport.CSVError on a bad file."""
    first = (f"{data_summary(csv_text, filename)}\n\n"
             f"What the user wants:\n{idea.strip()}")
    return {"version": 1, "model": MODEL, "filename": filename, "idea": idea.strip(),
            "messages": [{"role": "user", "content": first}],
            "steps": 0, "views": 0, "cost_usd": 0.0,
            "created": False, "done": False, "reply": "", "log": []}


def step(state, fig, csv_text):
    """One Claude call and the tools it asks for. Mutates and returns state.
    -> (state, spec or None if unchanged, events). Raises anthropic errors
    (the caller doesn't save anything then, so the step can be retried)."""
    if state["done"]:
        return state, None, []

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
    )
    state["steps"] += 1
    state["cost_usd"] = round(state["cost_usd"] + _cost(response.usage), 5)
    state["messages"].append({"role": "assistant",
                              "content": [b.to_dict() for b in response.content]})

    if response.stop_reason == "refusal":
        return _finish(state, "Claude declined to make this figure. Try describing it differently."), None, []

    calls = [b for b in response.content if b.type == "tool_use"]
    if not calls:
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            text = text or "Claude ran out of room before finishing."
        return _finish(state, text or "Done."), None, []

    spec = fig.load_spec() if state["created"] else None
    arrays = fig.load_arrays() if state["created"] else None
    changed, events, results = False, [], []
    for call in calls:
        try:
            content, spec, arrays, did_change = _run_tool(state, fig, csv_text, call, spec, arrays)
            changed |= did_change
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
            events.append(PROGRESS.get(call.name, call.name))
        except _ToolError as e:
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": str(e), "is_error": True})
    state["messages"].append({"role": "user", "content": results})
    state["log"] += events

    if state["steps"] >= MAX_STEPS or state["cost_usd"] >= MAX_COST_USD:
        why = "step" if state["steps"] >= MAX_STEPS else "cost"
        note = ("I stopped here to keep this figure's cost down." if why == "cost"
                else "I stopped after the maximum number of steps.")
        _finish(state, note + " You can keep adjusting it by hand.")
    return state, (spec if changed else None), events


def _finish(state, reply):
    state["done"], state["reply"] = True, reply
    return state


class _ToolError(Exception):
    pass


def _run_tool(state, fig, csv_text, call, spec, arrays):
    """-> (tool_result content, spec, arrays, changed)."""
    args = call.input or {}
    if call.name == "create_figure":
        if state["created"]:
            raise _ToolError("The figure already exists - change it with the editing tools.")
        panels = [{"x": p["x_column"], "ys": p["y_columns"], "kind": p["plot_as"],
                   "title": p["title"], "xlabel": p["x_label"], "ylabel": p["y_label"],
                   "xscale": p["x_scale"], "yscale": p["y_scale"]}
                  for p in args.get("panels") or []]
        try:
            new_spec, new_arrays = csvimport.build_panels(
                fig.name, csv_text, panels, state["filename"], args.get("figure_title") or "")
            render.render(new_spec, new_arrays)   # prove it draws before keeping it
        except csvimport.CSVError as e:
            raise _ToolError(f"Couldn't make that figure: {e}")
        except Exception as e:
            raise _ToolError(f"That figure couldn't be drawn ({type(e).__name__}: {e}).")
        fig.save_arrays(new_arrays)
        state["created"] = True
        summary = json.dumps(ops.describe(new_spec, new_arrays))
        return f"Created. The figure now:\n{summary}", new_spec, new_arrays, True

    if not state["created"]:
        raise _ToolError("There is no figure yet - call create_figure first.")

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
    response = _client().beta.messages.create(
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
