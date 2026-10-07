"""The kinds of curve a panel can hold -- one table the renderer, codegen,
codesync, the builder and the editor all read, so a new kind is one entry
here (plus editor fields).

A series in the spec is {"id", "kind"?, "label"?, "style", <data fields>,
"colorbar"?}. Each data field is an npz key (or an inline list); a "list"
field is a list of keys (box plots: one array per group). "kind" is omitted
for plain lines, so figures made before kinds existed are unchanged.

    method     the Axes method that draws it (and that codesync recognises)
    coords     positional data fields, in order
    lists      positional fields that hold a LIST of keys (box: groups)
    data_kw    keyword data fields: {matplotlib kwarg: spec field}
    style      style keys passed straight through to matplotlib
    fixed      keyword arguments always passed (codegen writes them too)
    out / back optional style translation: spec style -> matplotlib kwargs
               and back (a box's "color" is boxprops=dict(facecolor=...))
    colorbar   the kind draws colours from a colour map and can have a bar
    grid       z is 2-D (heatmaps, contours): x and y are its axes
    words      what the editor calls it
"""

LINE_STYLE = {"color", "lw", "linewidth", "ls", "linestyle", "marker", "ms",
              "markersize", "mew", "markeredgewidth", "mfc", "markerfacecolor",
              "mec", "markeredgecolor", "alpha", "zorder", "drawstyle"}
CMAP_STYLE = {"cmap", "vmin", "vmax", "norm", "alpha", "zorder"}

# Colour maps offered in the editor and to Claude (all perceptually sound or
# standard diverging ones; any matplotlib name still works from code).
COLORMAPS = ["viridis", "magma", "inferno", "plasma", "cividis", "turbo",
             "coolwarm", "RdBu_r", "seismic", "Greys", "Blues", "hot"]


def _box_out(style):
    """Spec style -> boxplot kwargs: the editor's color/alpha/lw become
    the box faces' properties; everything else passes through."""
    st = dict(style)
    box = {}
    for key, prop in (("color", "facecolor"), ("alpha", "alpha"), ("edgecolor", "edgecolor"),
                      ("lw", "linewidth")):
        if key in st:
            box[prop] = st.pop(key)
    if box:
        st["boxprops"] = box
    return st


def _box_back(kwargs):
    st = dict(kwargs)
    box = st.pop("boxprops", None) or {}
    for key, prop in (("color", "facecolor"), ("alpha", "alpha"), ("edgecolor", "edgecolor"),
                      ("lw", "linewidth")):
        if prop in box:
            st[key] = box[prop]
    return st


KINDS = {
    "line": {
        "method": "plot", "coords": ("x", "y"), "data_kw": {},
        "style": LINE_STYLE, "words": "line",
    },
    "step": {
        "method": "step", "coords": ("x", "y"), "data_kw": {},
        "style": LINE_STYLE | {"where"}, "words": "steps",
    },
    "errorbar": {
        "method": "errorbar", "coords": ("x", "y"), "data_kw": {"yerr": "yerr", "xerr": "xerr"},
        "style": LINE_STYLE | {"capsize", "elinewidth", "ecolor", "capthick"},
        "words": "error bars",
    },
    "band": {
        "method": "fill_between", "coords": ("x", "y", "y2"), "data_kw": {},
        "style": {"color", "alpha", "lw", "linewidth", "ls", "linestyle", "zorder",
                  "edgecolor", "hatch"},
        "words": "band",
    },
    "bar": {
        "method": "bar", "coords": ("x", "y"), "data_kw": {},
        "style": {"color", "alpha", "width", "edgecolor", "lw", "linewidth", "zorder",
                  "hatch", "align"},
        "words": "bars",
    },
    "scatter": {
        "method": "scatter", "coords": ("x", "y"), "data_kw": {"c": "c", "s": "sizes"},
        "style": CMAP_STYLE | {"color", "s", "marker", "edgecolors", "linewidths"},
        "colorbar": True, "words": "coloured dots",
    },
    "heatmap": {
        "method": "pcolormesh", "coords": ("x", "y", "z"), "data_kw": {},
        "style": CMAP_STYLE, "fixed": {"shading": "auto"},
        "colorbar": True, "grid": True, "words": "heatmap",
    },
    "contour": {
        "method": "contour", "coords": ("x", "y", "z"), "data_kw": {},
        "style": CMAP_STYLE | {"levels", "colors", "linewidths", "linestyles"},
        "colorbar": True, "grid": True, "words": "contour lines",
    },
    "contourf": {
        "method": "contourf", "coords": ("x", "y", "z"), "data_kw": {},
        "style": CMAP_STYLE | {"levels"},
        "colorbar": True, "grid": True, "words": "filled contours",
    },
    "box": {
        "method": "boxplot", "coords": (), "lists": ("groups",), "data_kw": {},
        "style": {"boxprops", "positions", "widths", "tick_labels", "notch", "showfliers",
                  "whis", "medianprops", "zorder"},
        "fixed": {"patch_artist": True},
        "out": _box_out, "back": _box_back, "words": "box plot",
    },
}

BY_METHOD = {k["method"]: name for name, k in KINDS.items()}

# Kinds whose label goes in the legend (heatmaps and contours use a colour
# bar instead, so their name isn't written into the code).
LEGEND_KINDS = {"line", "step", "errorbar", "band", "bar", "scatter", "box"}

# Every style key any kind draws -- what codesync treats as "spelled out in
# code" (plus the editor-level keys translated by "out").
ALL_STYLE = set().union(*(k["style"] for k in KINDS.values())) | {"color", "alpha", "edgecolor", "lw"}


def kind_of(series):
    return series.get("kind") or "line"


def info(series):
    return KINDS[kind_of(series)]


def data_fields(series):
    """Every spec field of this series that holds data."""
    k = info(series)
    return (list(k["coords"]) + list(k.get("lists", ()))
            + [f for f in k["data_kw"].values() if series.get(f) is not None])


def style_for(series):
    """matplotlib keyword arguments for this series' style: the keys its
    kind draws, translated (box), minus any that a data field supplies."""
    k = info(series)
    st = dict(series.get("style") or {})
    if "out" in k:
        st = k["out"](st)
    st = {key: v for key, v in st.items() if key in k["style"]}
    for kwname, field in k["data_kw"].items():
        if series.get(field) is not None:
            st.pop(kwname, None)
    return st


def style_from_kwargs(kind, kwargs):
    """codesync: matplotlib kwargs read from code -> the spec's style."""
    k = KINDS[kind]
    st = {key: v for key, v in kwargs.items() if key in k["style"]}
    return k["back"](st) if "back" in k else st
