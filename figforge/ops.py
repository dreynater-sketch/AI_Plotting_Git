"""Figure-editing operations: the groundwork for a future AI assistant.

Every edit an assistant (or a script) might make to a figure is a named,
validated operation on the spec. TOOLS below are those operations written
as Claude API tool definitions (name / description / input_schema, with
strict: true so the model's arguments always match the schema); apply()
runs a list of calls against a spec and returns one result per call, in the
shape a tool_result needs (content text + is_error). describe() is the
compact view of a figure the assistant reads first.

How the assistant plugs in later (nothing here calls a model yet): a
server-side loop sends the user's request plus TOOLS to the Messages API
(model "claude-opus-5"), runs every tool_use block through apply() via
POST /api/ops/<project>, and returns all results in one user message of
tool_result blocks until the model stops asking for tools. The API key stays
on the server; the browser never talks to Anthropic directly.

Strict-mode schemas can't carry numeric ranges or string lengths, so every
limit is enforced here, and a bad argument comes back as an is_error result
the model can correct -- it never reaches the spec.
"""

import copy
import math

from matplotlib.colors import is_color_like

# ------------------------------------------------------------------ schemas

def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props, description=None):
    """A strict object: every property listed as required (optional ones are
    nullable), and no extra properties."""
    out = {"type": "object", "properties": props, "required": list(props),
           "additionalProperties": False}
    if description:
        out["description"] = description
    return out


_ID = {"type": "string", "description": "An element id from describe_figure."}
_PANEL = {"type": "string", "description": "A panel id from describe_figure, e.g. \"a\"."}
_COLOR = {"type": "string", "description": "A matplotlib colour: \"#1f77b4\", \"black\", \"0.5\" (grey)."}
# Strict mode allows at most 16 nullable (union-typed) parameters across all
# tools, so optional *strings* say "unchanged" with a value instead of null:
# "" for a colour, "keep" in an enum. Numbers stay nullable.
_KEEP_COLOR = {"type": "string",
               "description": "A matplotlib colour (\"#1f77b4\", \"black\", \"0.5\"), or \"\" to leave it unchanged."}


def _keep_enum(values):
    return {"type": "string", "enum": ["keep"] + list(values),
            "description": "\"keep\" leaves it unchanged."}


def _given(v):
    """A value that means "change it": not null, "" or "keep"."""
    return v is not None and v != "" and v != "keep"


_COORDS = {"type": "string", "enum": ["data", "axes"],
           "description": "\"data\": x/y in the panel's data units. \"axes\": 0-1 across the panel box."}

TOOLS = [
    {
        "name": "describe_figure",
        "description": "Read the current figure: every panel, its axes, and each label, curve, "
                       "arrow and legend with its id, position and style. Call this first, and "
                       "again after edits if you need fresh ids or values.",
        "input_schema": _obj({}),
    },
    {
        "name": "set_text",
        "description": "Change the text of a label, a panel title (\"<panel>__title\"), an axis label "
                       "(\"<panel>__xlabel\" / \"<panel>__ylabel\"), the figure title (\"suptitle\"), or a "
                       "curve's legend entry (the curve's id). Matplotlib mathtext like $\\alpha_1$ works; "
                       "an empty string hides a title or axis label.",
        "input_schema": _obj({"element_id": _ID, "text": {"type": "string"}}),
    },
    {
        "name": "style_text",
        "description": "Font size (pt, 4-72) and/or colour of any text element: labels, titles, axis "
                       "labels, the figure title. Pass null to leave a property unchanged.",
        "input_schema": _obj({"element_id": _ID,
                              "size": _nullable({"type": "number"}),
                              "color": _KEEP_COLOR}),
    },
    {
        "name": "move_element",
        "description": "Move a free label or a legend. Labels take x/y in data or axes coordinates; "
                       "a legend's position is its upper-left corner in axes coordinates (0-1).",
        "input_schema": _obj({"element_id": _ID, "x": {"type": "number"}, "y": {"type": "number"},
                              "coords": _COORDS}),
    },
    {
        "name": "add_label",
        "description": "Add a new free text label to a panel. Returns the new label's id.",
        "input_schema": _obj({"panel_id": _PANEL, "text": {"type": "string"},
                              "x": {"type": "number"}, "y": {"type": "number"}, "coords": _COORDS,
                              "size": _nullable({"type": "number"}), "color": _KEEP_COLOR}),
    },
    {
        "name": "delete_element",
        "description": "Remove a free label, arrow, curve or legend. Titles and axis labels are "
                       "hidden (their style is kept) -- use set_text with \"\" for the same effect.",
        "input_schema": _obj({"element_id": _ID}),
    },
    {
        "name": "set_axis",
        "description": "Change one axis of a panel: limits, linear/log scale, tick label size. "
                       "Pass null for anything to leave unchanged. Log scale needs positive limits.",
        "input_schema": _obj({"panel_id": _PANEL, "axis": {"type": "string", "enum": ["x", "y", "right"]},
                              "min": _nullable({"type": "number"}), "max": _nullable({"type": "number"}),
                              "scale": _keep_enum(["linear", "log"]),
                              "tick_size": _nullable({"type": "number"})}),
    },
    {
        "name": "style_series",
        "description": "Restyle a curve. Pass null for anything to leave unchanged. line_style \"none\" "
                       "draws markers only; marker \"none\" draws the line only; opacity is 0-1.",
        "input_schema": _obj({
            "series_id": _ID,
            "color": _KEEP_COLOR,
            "line_width": _nullable({"type": "number"}),
            "line_style": _keep_enum(["-", "--", ":", "-.", "none"]),
            "marker": _keep_enum(["none", "o", ".", "s", "^", "v", "D", "x", "+", "*"]),
            "marker_size": _nullable({"type": "number"}),
            "opacity": _nullable({"type": "number"}),
        }),
    },
    {
        "name": "set_legend",
        "description": "Show or hide a panel's legend and set its placement, font size and frame. "
                       "Pass null to leave a property unchanged. Only curves with a legend label appear.",
        "input_schema": _obj({
            "panel_id": _PANEL,
            "visible": {"type": "boolean"},
            "location": _keep_enum([
                "best", "upper right", "upper left", "lower left", "lower right", "right",
                "center left", "center right", "lower center", "upper center", "center"]),
            "font_size": _nullable({"type": "number"}),
            "frame": _nullable({"type": "boolean"}),
        }),
    },
    {
        "name": "set_second_axis",
        "description": "A second scale on the top (for x) or right (for y) edge of a panel, for the "
                       "same data in other units: second value = scale * main value + offset. E.g. "
                       "channel along the bottom and energy on top with the detector calibration. "
                       "show false removes it. Its label's id is \"<panel>__top_axis\" or "
                       "\"<panel>__right_axis\" (set_text / style_text work on it).",
        "input_schema": _obj({"panel_id": _PANEL, "side": {"type": "string", "enum": ["top", "right"]},
                              "show": {"type": "boolean"}, "label": {"type": "string"},
                              "scale": {"type": "number"}, "offset": {"type": "number"}}),
    },
    {
        "name": "style_colormap",
        "description": "Colours of a heatmap, contour, filled contour or coloured scatter: the colour "
                       "map, the value range it spans, a log colour scale, and its colour bar. "
                       "null or \"keep\" leaves a setting unchanged.",
        "input_schema": _obj({
            "series_id": _ID,
            "colormap": _keep_enum(["viridis", "magma", "inferno", "plasma", "cividis", "turbo",
                                    "coolwarm", "RdBu_r", "seismic", "Greys", "Blues", "hot"]),
            "min": _nullable({"type": "number"}), "max": _nullable({"type": "number"}),
            "log_scale": _keep_enum(["on", "off"]),
            "colorbar": _keep_enum(["show", "hide"]),
            "colorbar_label": {"type": "string", "description": "\"keep\" leaves it unchanged."},
        }),
    },
    {
        "name": "set_grid",
        "description": "Grid lines and small (minor) ticks of a panel. grid: \"none\", \"major\" "
                       "(at the numbered ticks) or \"both\" (also between them).",
        "input_schema": _obj({"panel_id": _PANEL,
                              "grid": {"type": "string", "enum": ["none", "major", "both"]},
                              "minor_ticks": {"type": "boolean"}}),
    },
    {
        "name": "set_tick_format",
        "description": "How one axis numbers its ticks: scientific notation (x10^n at the axis end) "
                       "and/or a tick every `step` data units (null = automatic).",
        "input_schema": _obj({"panel_id": _PANEL, "axis": {"type": "string", "enum": ["x", "y"]},
                              "scientific": {"type": "boolean"},
                              "step": _nullable({"type": "number"})}),
    },
    {
        "name": "add_span",
        "description": "Shade a range of a panel behind the data: axis \"x\" shades from..to "
                       "along x (a vertical band), \"y\" along y. label \"\" keeps it out of the "
                       "legend. Its id is \"<panel>__span_<n>\" (delete_element removes it).",
        "input_schema": _obj({"panel_id": _PANEL, "axis": {"type": "string", "enum": ["x", "y"]},
                              "from": {"type": "number"}, "to": {"type": "number"},
                              "color": _KEEP_COLOR, "opacity": _nullable({"type": "number"}),
                              "label": {"type": "string"}}),
    },
    {
        "name": "set_curve_axis",
        "description": "Put a curve on the left y-axis or on a second, independent y-axis on the "
                       "right (for a second quantity with other units against the same x). The "
                       "right axis's label id is \"<panel>__ylabel2\"; set_axis with axis "
                       "\"right\" sets its range and scale.",
        "input_schema": _obj({"series_id": _ID, "axis": {"type": "string", "enum": ["left", "right"]}}),
    },
    {
        "name": "set_layout",
        "description": "Arrange the panels in a grid: rows x columns, filled left to right then "
                       "down (rows 1 = all side by side). rows x columns must hold every panel.",
        "input_schema": _obj({"rows": {"type": "integer"}, "columns": {"type": "integer"}}),
    },
    {
        "name": "set_font",
        "description": "The typeface for all text in the figure.",
        "input_schema": _obj({"family": {"type": "string", "enum": ["sans-serif", "serif", "monospace"]}}),
    },
    {
        "name": "set_figure_size",
        "description": "The whole figure's size in inches (1-40 each way).",
        "input_schema": _obj({"width_in": {"type": "number"}, "height_in": {"type": "number"}}),
    },
]
for _tool in TOOLS:
    _tool["strict"] = True

TOOL_NAMES = {t["name"] for t in TOOLS}


# ----------------------------------------------------------------- lookup

class OpError(Exception):
    """A call that can't be applied; its message goes back to the caller."""


def _panel(spec, pid):
    for p in spec["panels"]:
        if p["id"] == pid:
            return p
    raise OpError(f"no panel '{pid}' (panels: {', '.join(p['id'] for p in spec['panels'])})")


def _find(spec, eid):
    """-> (kind, object, panel) for an element id, as the editor names them."""
    if eid == "suptitle":
        return "suptitle", spec.setdefault("suptitle", {}), None
    if "__" in eid:
        pid, part = eid.split("__", 1)
        p = _panel(spec, pid)
        if part in ("title", "xlabel", "ylabel"):
            return part, p.setdefault(part, {}), p
        if part == "ylabel2":
            if p.get("twin") is None:
                raise OpError(f"panel {pid} has no right axis")
            return part, p.setdefault("ylabel2", {}), p
        if part.startswith("span_") and part[5:].isdigit():
            spans = p.get("spans") or []
            i = int(part[5:])
            if i >= len(spans):
                raise OpError(f"panel {pid} has no shaded region {i}")
            return "span", spans[i], p
        if part in ("top_axis", "right_axis"):
            if not p.get(part):
                raise OpError(f"panel {pid} has no {part.replace('_', ' ')} -- add it with set_second_axis")
            return part, p[part], p
        if part == "legend":
            return "legend", p.get("legend"), p
    for p in spec["panels"]:
        for kind in ("texts", "series", "arrows"):
            for item in p.get(kind, []):
                if item["id"] == eid:
                    return kind[:-1] if kind != "series" else "series", item, p
    raise OpError(f"no element '{eid}' -- call describe_figure for current ids")


def _num(v, what, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise OpError(f"{what} must be a finite number")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise OpError(f"{what} must be between {lo} and {hi}")
    return v


def _color(v):
    if not isinstance(v, str) or not is_color_like(v):
        raise OpError(f"'{v}' isn't a colour matplotlib understands")
    return v


def _mathtext_ok(text):
    if text.replace(r"\$", "").count("$") % 2:
        raise OpError("unbalanced $ -- mathtext needs $...$ pairs (use \\$ for a literal dollar)")
    return text


# -------------------------------------------------------------- operations

def _second_desc(p, key):
    sa = p.get(key)
    if not sa:
        return None
    return {"label_id": f"{p['id']}__{key}", "label": sa.get("text", ""),
            "scale": sa["scale"], "offset": sa["offset"]}


def _points(s, arrays):
    """How many values a curve draws (a heatmap: cells; a box plot: all groups)."""
    refs = s.get("groups") or [s.get("z") or s.get("y")]
    n = 0
    for r in refs:
        if isinstance(r, str) and r in arrays:
            n += int(arrays[r].size)
        elif isinstance(r, list):
            n += len(r)
    return n


def _describe(spec, arrays):
    def rng(key):
        a = arrays.get(key) if isinstance(key, str) else None
        if a is None:
            return None
        good = a[~(a != a)]
        return [round(float(good.min()), 6), round(float(good.max()), 6)] if good.size else None

    out = {"size_in": spec.get("size_in"), "grid_shape": spec.get("grid_shape") or [1, len(spec["panels"])],
           "font": (spec.get("rcparams") or {}).get("font.family", "sans-serif"),
           "title": (spec.get("suptitle") or {}).get("text", ""),
           "panels": []}
    for p in spec["panels"]:
        panel = {
            "id": p["id"],
            "title": {"id": f"{p['id']}__title", "text": (p.get("title") or {}).get("text", "")},
            "x_axis": {"label_id": f"{p['id']}__xlabel", "label": (p.get("xlabel") or {}).get("text", ""),
                       "min": p["xlim"][0], "max": p["xlim"][1], "scale": p.get("xscale", "linear")},
            "y_axis": {"label_id": f"{p['id']}__ylabel", "label": (p.get("ylabel") or {}).get("text", ""),
                       "min": p["ylim"][0], "max": p["ylim"][1], "scale": p.get("yscale", "linear")},
            "right_y_axis": None if p.get("twin") is None else {
                "label_id": f"{p['id']}__ylabel2", "label": (p.get("ylabel2") or {}).get("text", ""),
                **p["twin"]},
            "grid": p.get("grid", "none"), "minor_ticks": bool(p.get("minor_ticks")),
            "x_ticks": p.get("xfmt") or "automatic", "y_ticks": p.get("yfmt") or "automatic",
            "shaded_regions": [{"id": f"{p['id']}__span_{i}", **sp} for i, sp in enumerate(p.get("spans", []))],
            "top_axis": _second_desc(p, "top_axis"),
            "right_axis": _second_desc(p, "right_axis"),
            "legend": None if not p.get("legend") else {
                "id": f"{p['id']}__legend", "location": p["legend"].get("xy") or p["legend"].get("loc", "best"),
                "font_size": p["legend"].get("size", 10), "frame": p["legend"].get("frameon", False)},
            "labels": [{"id": t["id"], "text": t["text"], "x": t["xy"][0], "y": t["xy"][1],
                        "coords": t.get("coords", "data"), "size": t.get("size", 12),
                        "color": t.get("color", "black")} for t in p.get("texts", [])],
            "curves": [{"id": s["id"], "kind": s.get("kind", "line"), "legend_label": s.get("label", ""),
                        "y_axis": s.get("axis", "left"),
                        "colorbar": s.get("colorbar"),
                        "points": _points(s, arrays),
                        "x_range": rng(s.get("x")), "y_range": rng(s.get("y")),
                        **({"z_range": rng(s["z"])} if s.get("z") else {}),
                        "style": s.get("style", {})} for s in p.get("series", [])],
            "arrows": [{"id": a["id"], "from": a["p0"], "to": a["p1"],
                        "color": a.get("color", "black")} for a in p.get("arrows", [])],
        }
        out["panels"].append(panel)
    return out


_TEXT_KINDS = ("text", "title", "xlabel", "ylabel", "suptitle", "top_axis", "right_axis", "ylabel2")


def _set_text(spec, a):
    kind, obj, _ = _find(spec, a["element_id"])
    text = _mathtext_ok(a["text"])
    if kind == "series":
        obj["label"] = text
    elif kind in _TEXT_KINDS:
        obj["text"] = text
    else:
        raise OpError(f"'{a['element_id']}' has no text to set")
    return f"text of {a['element_id']} set"


def _style_text(spec, a):
    kind, obj, _ = _find(spec, a["element_id"])
    if kind not in _TEXT_KINDS:
        raise OpError(f"'{a['element_id']}' isn't a text element")
    if a["size"] is not None:
        obj["size"] = _num(a["size"], "size", 4, 72)
    if _given(a["color"]):
        obj["color"] = _color(a["color"])
    return f"{a['element_id']} restyled"


def _move(spec, a):
    kind, obj, p = _find(spec, a["element_id"])
    x, y = _num(a["x"], "x"), _num(a["y"], "y")
    if kind == "text":
        obj["xy"] = [x, y]
        obj["coords"] = a["coords"]
    elif kind == "legend":
        if obj is None:
            raise OpError("that panel's legend is hidden -- set_legend visible=true first")
        if a["coords"] != "axes":
            raise OpError("a legend is placed in axes coordinates (0-1)")
        obj["xy"] = [x, y]
    else:
        raise OpError(f"'{a['element_id']}' can't be moved (only free labels and legends)")
    return f"{a['element_id']} moved to ({x}, {y}) {a['coords']}"


def _add_label(spec, a):
    p = _panel(spec, a["panel_id"])
    ids = {i["id"] for q in spec["panels"] for k in ("texts", "series", "arrows") for i in q.get(k, [])}
    n = 1
    while f"{p['id']}_label{n}" in ids:
        n += 1
    new = {"id": f"{p['id']}_label{n}", "text": _mathtext_ok(a["text"]),
           "xy": [_num(a["x"], "x"), _num(a["y"], "y")], "coords": a["coords"],
           "size": _num(a["size"], "size", 4, 72) if a["size"] is not None else 12,
           "color": _color(a["color"]) if _given(a["color"]) else "#000000",
           "ha": "left", "va": "baseline"}
    p.setdefault("texts", []).append(new)
    return f"added label {new['id']}"


def _delete(spec, a):
    kind, obj, p = _find(spec, a["element_id"])
    if kind in ("text", "arrow", "series"):
        key = kind + "s" if kind != "series" else "series"
        p[key] = [i for i in p[key] if i["id"] != a["element_id"]]
    elif kind == "legend":
        p["legend"] = None
    elif kind == "span":
        p["spans"] = [x for x in p["spans"] if x is not obj]
    else:
        obj["text"] = ""
    return f"{a['element_id']} removed"


def _set_axis(spec, a):
    p = _panel(spec, a["panel_id"])
    ax = a["axis"]
    if ax == "right":
        return _set_twin_axis(p, a)
    lim = list(p[f"{ax}lim"])
    if a["min"] is not None:
        lim[0] = _num(a["min"], "min")
    if a["max"] is not None:
        lim[1] = _num(a["max"], "max")
    if lim[0] == lim[1]:
        raise OpError("min and max can't be equal")
    scale = a["scale"] if _given(a["scale"]) else p.get(f"{ax}scale", "linear")
    if scale == "log" and (lim[0] <= 0 or lim[1] <= 0):
        raise OpError("log scale needs both limits above zero")
    p[f"{ax}lim"], p[f"{ax}scale"] = lim, scale
    if a["tick_size"] is not None:
        p[f"{ax}tick_size"] = _num(a["tick_size"], "tick_size", 4, 48)
    return f"panel {p['id']} {ax} axis: {lim[0]} to {lim[1]}, {scale}"


def _style_series(spec, a):
    kind, s, _ = _find(spec, a["series_id"])
    if kind != "series":
        raise OpError(f"'{a['series_id']}' isn't a curve")
    st = dict(s.get("style") or {})
    if _given(a["color"]):
        st["color"] = _color(a["color"])
    if a["line_width"] is not None:
        st["lw"] = _num(a["line_width"], "line_width", 0, 20)
    if _given(a["line_style"]):
        st["ls"] = a["line_style"]
    if _given(a["marker"]):
        st["marker"] = "" if a["marker"] == "none" else a["marker"]
    if a["marker_size"] is not None:
        st["ms"] = _num(a["marker_size"], "marker_size", 0, 40)
    if a["opacity"] is not None:
        st["alpha"] = _num(a["opacity"], "opacity", 0, 1)
    s["style"] = st
    return f"{a['series_id']} restyled"


def _set_legend(spec, a):
    p = _panel(spec, a["panel_id"])
    if not a["visible"]:
        p["legend"] = None
        return f"legend of panel {p['id']} hidden"
    lg = dict(p.get("legend") or {"loc": "best", "frameon": False, "size": 10, "xy": None})
    if _given(a["location"]):
        lg["loc"], lg["xy"] = a["location"], None
    if a["font_size"] is not None:
        lg["size"] = _num(a["font_size"], "font_size", 4, 48)
    if a["frame"] is not None:
        lg["frameon"] = bool(a["frame"])
    p["legend"] = lg
    note = "" if any(s.get("label") for s in p.get("series", [])) else \
        " (no curve has a legend label yet, so nothing shows -- set_text on a curve id adds one)"
    return f"legend of panel {p['id']} updated{note}"


def _set_second_axis(spec, a):
    p = _panel(spec, a["panel_id"])
    key = "top_axis" if a["side"] == "top" else "right_axis"
    if key == "right_axis" and a["show"] and p.get("twin") is not None:
        raise OpError(f"panel {p['id']}'s right side already holds a second y-axis with its own data")
    if not a["show"]:
        p.pop(key, None)
        return f"panel {p['id']}: {a['side']} axis removed"
    scale = _num(a["scale"], "scale")
    if scale == 0:
        raise OpError("scale can't be 0")
    old = p.get(key) or {"size": 13, "color": "#000000"}
    p[key] = {**old, "text": _mathtext_ok(a["label"]), "scale": scale,
              "offset": _num(a["offset"], "offset")}
    return f"panel {p['id']}: {a['side']} axis = {scale} * value + {p[key]['offset']} (label id {p['id']}__{key})"


def _style_colormap(spec, a):
    from figforge import layers
    kind, s, _ = _find(spec, a["series_id"])
    if kind != "series" or not layers.info(s).get("colorbar"):
        raise OpError(f"'{a['series_id']}' isn't a heatmap, contour or coloured scatter")
    st = dict(s.get("style") or {})
    if _given(a["colormap"]):
        st["cmap"] = a["colormap"]
        st.pop("colors", None)      # contour lines in one colour -> now by level
    for key in ("min", "max"):
        if a[key] is not None:
            st["vmin" if key == "min" else "vmax"] = _num(a[key], key)
    if a["log_scale"] == "on":
        st["norm"] = "log"
    elif a["log_scale"] == "off":
        st.pop("norm", None)
    s["style"] = st
    if a["colorbar"] == "show":
        s["colorbar"] = dict(s.get("colorbar") or {"label": s.get("label", "")})
    elif a["colorbar"] == "hide":
        s.pop("colorbar", None)
    if a["colorbar_label"] != "keep" and s.get("colorbar") is not None:
        s["colorbar"]["label"] = _mathtext_ok(a["colorbar_label"])
    return f"{a['series_id']} colours updated"


def _set_grid(spec, a):
    p = _panel(spec, a["panel_id"])
    if a["grid"] == "none":
        p.pop("grid", None)
    else:
        p["grid"] = a["grid"]
    if a["minor_ticks"]:
        p["minor_ticks"] = True
    else:
        p.pop("minor_ticks", None)
    return f"panel {p['id']}: grid {a['grid']}, minor ticks {'on' if a['minor_ticks'] else 'off'}"


def _set_tick_format(spec, a):
    p = _panel(spec, a["panel_id"])
    key = f"{a['axis']}fmt"
    f = {}
    if a["scientific"]:
        f["sci"] = True
    if a["step"] is not None:
        step = _num(a["step"], "step")
        lim = p[f"{a['axis']}lim"]
        if step <= 0 or abs(lim[1] - lim[0]) / step > 200:
            raise OpError(f"a step of {step} would put more than 200 ticks on that axis")
        f["step"] = step
    if f:
        p[key] = f
    else:
        p.pop(key, None)
    return f"panel {p['id']} {a['axis']} ticks: {f or 'automatic'}"


def _add_span(spec, a):
    p = _panel(spec, a["panel_id"])
    lo, hi = sorted((_num(a["from"], "from"), _num(a["to"], "to")))
    if lo == hi:
        raise OpError("from and to can't be equal")
    sp = {"axis": a["axis"], "lo": lo, "hi": hi,
          "color": _color(a["color"]) if _given(a["color"]) else "#cccccc",
          "alpha": _num(a["opacity"], "opacity", 0, 1) if a["opacity"] is not None else 0.25}
    if a["label"]:
        sp["label"] = _mathtext_ok(a["label"])
    p.setdefault("spans", []).append(sp)
    return f"added shaded region {p['id']}__span_{len(p['spans']) - 1}"


def _set_twin_axis(p, a):
    if p.get("twin") is None:
        raise OpError(f"panel {p['id']} has no right axis -- put a curve on it with set_curve_axis")
    tw = p["twin"]
    lim = list(tw.get("ylim") or [None, None])
    if a["min"] is not None:
        lim[0] = _num(a["min"], "min")
    if a["max"] is not None:
        lim[1] = _num(a["max"], "max")
    if None not in lim and lim[0] == lim[1]:
        raise OpError("min and max can't be equal")
    if any(v is not None for v in lim):
        tw["ylim"] = lim   # a None end is picked automatically
    scale = a["scale"] if _given(a["scale"]) else tw.get("yscale", "linear")
    if scale == "log" and any(v is not None and v <= 0 for v in lim):
        raise OpError("log scale needs both limits above zero")
    if _given(a["scale"]):
        tw["yscale"] = a["scale"]
    if a["tick_size"] is not None:
        tw["tick_size"] = _num(a["tick_size"], "tick_size", 4, 48)
    return f"panel {p['id']} right axis: {tw}"


def _set_curve_axis(spec, a):
    kind, s, p = _find(spec, a["series_id"])
    if kind != "series":
        raise OpError(f"'{a['series_id']}' isn't a curve")
    if a["axis"] == "right":
        if p.get("right_axis"):
            raise OpError(f"panel {p['id']} already has a right-hand scale (a calibration); remove "
                          "it with set_second_axis show false first")
        s["axis"] = "right"
        if p.get("twin") is None:
            p["twin"] = {}
            p["ylabel2"] = {"text": s.get("label", ""), "size": 13, "color": "#000000"}
    else:
        s.pop("axis", None)
        if not any(c.get("axis") == "right" for c in p.get("series", [])):
            p.pop("twin", None)
            p.pop("ylabel2", None)
    return f"{a['series_id']} is on the {a['axis']} axis"


def _set_layout(spec, a):
    n = len(spec["panels"])
    rows, cols = int(_num(a["rows"], "rows", 1, 9)), int(_num(a["columns"], "columns", 1, 9))
    if rows * cols < n:
        raise OpError(f"{rows} x {cols} holds {rows * cols} panels, but there are {n}")
    if (rows, cols) == (1, n):
        spec.pop("grid_shape", None)
    else:
        spec["grid_shape"] = [rows, cols]
    return f"panels arranged {rows} x {cols} (resize the figure with set_figure_size if needed)"


def _set_font(spec, a):
    rc = dict(spec.get("rcparams") or {})
    if a["family"] == "sans-serif":
        rc.pop("font.family", None)
    else:
        rc["font.family"] = a["family"]
    # Math ($...$) matches the text: Computer Modern-like for serif.
    if a["family"] == "serif":
        rc["mathtext.fontset"] = "dejavuserif"
    else:
        rc.pop("mathtext.fontset", None)
    spec["rcparams"] = rc
    return f"font is now {a['family']}"


def _set_size(spec, a):
    spec["size_in"] = [_num(a["width_in"], "width_in", 1, 40), _num(a["height_in"], "height_in", 1, 40)]
    return f"figure is now {spec['size_in'][0]} x {spec['size_in'][1]} in"


_HANDLERS = {"set_text": _set_text, "style_text": _style_text, "move_element": _move,
             "add_label": _add_label, "delete_element": _delete, "set_axis": _set_axis,
             "style_series": _style_series, "set_legend": _set_legend, "set_figure_size": _set_size,
             "set_second_axis": _set_second_axis, "set_font": _set_font,
             "style_colormap": _style_colormap, "set_grid": _set_grid,
             "set_tick_format": _set_tick_format, "add_span": _add_span, "set_layout": _set_layout,
             "set_curve_axis": _set_curve_axis}


def _check_input(name, args):
    """What strict mode guarantees for model calls, checked for every caller:
    exactly the schema's properties, JSON-typed."""
    schema = next(t for t in TOOLS if t["name"] == name)["input_schema"]
    if not isinstance(args, dict):
        raise OpError("input must be an object")
    extra = set(args) - set(schema["properties"])
    missing = set(schema["properties"]) - set(args)
    if extra or missing:
        raise OpError(f"unexpected {sorted(extra)} / missing {sorted(missing)} "
                      "(pass null, \"\" or \"keep\" for 'unchanged')")


def describe(spec, arrays):
    return _describe(spec, arrays)


def apply(spec, calls, arrays=None):
    """Run [{"name", "input"}, ...] in order on a copy of the spec.
    -> (new_spec, results, changed). Each result is {"name", "is_error",
    "content"} -- one per call, as tool_result blocks need. A failing call
    changes nothing; the calls after it still run."""
    spec = copy.deepcopy(spec)
    results, changed = [], False
    for call in calls:
        name = call.get("name") if isinstance(call, dict) else None
        try:
            if name not in TOOL_NAMES:
                raise OpError(f"unknown tool '{name}'")
            args = call.get("input") or {}
            _check_input(name, args)
            if name == "describe_figure":
                import json
                results.append({"name": name, "is_error": False,
                                "content": json.dumps(_describe(spec, arrays or {}))})
                continue
            trial = copy.deepcopy(spec)
            msg = _HANDLERS[name](trial, args)
            spec, changed = trial, True
            results.append({"name": name, "is_error": False, "content": msg})
        except OpError as e:
            results.append({"name": name, "is_error": True, "content": str(e)})
    return spec, results, changed
