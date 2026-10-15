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
    proj       the panel projection the kind needs ("3d"); kinds without it
               draw on ordinary and polar panels
    words      what the editor calls it

A panel's "projection" is "3d" (x, y and z axes; "view" {elev, azim} turns
it) or "polar" (x is the angle in radians, y the distance from the middle);
without one it is an ordinary flat plot.
"""

import numpy as np

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


def _violin_out(style):
    """Spec style -> violinplot kwargs: color/edgecolor are the bodies' fill
    and outline colours."""
    st = dict(style)
    if "color" in st:
        st["facecolor"] = st.pop("color")
    if "edgecolor" in st:
        st["linecolor"] = st.pop("edgecolor")
    return st


def _violin_back(kwargs):
    st = dict(kwargs)
    if "facecolor" in st:
        st["color"] = st.pop("facecolor")
    if "linecolor" in st:
        st["edgecolor"] = st.pop("linecolor")
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
    "violin": {
        "method": "violinplot", "coords": (), "lists": ("groups",), "data_kw": {},
        "style": {"facecolor", "linecolor", "positions", "widths", "showmeans", "showmedians",
                  "showextrema"},
        "out": _violin_out, "back": _violin_back, "words": "violin plot",
    },
    "hist": {
        "method": "hist", "coords": ("x",), "data_kw": {},
        "style": {"bins", "color", "alpha", "edgecolor", "lw", "linewidth", "histtype",
                  "density", "cumulative", "log", "zorder", "hatch", "orientation"},
        "words": "histogram",
    },
    "quiver": {
        "method": "quiver", "coords": ("x", "y", "u", "v"), "data_kw": {},
        "style": {"color", "alpha", "scale", "width", "headwidth", "headlength", "pivot",
                  "angles", "scale_units", "zorder"},
        "words": "arrows",
    },
    "hexbin": {
        "method": "hexbin", "coords": ("x", "y"), "data_kw": {"C": "c"},
        "style": CMAP_STYLE | {"gridsize", "mincnt", "edgecolors", "linewidths", "bins"},
        "colorbar": True, "words": "hexagon map",
    },
    # 3-D: these need a panel with "projection": "3d".
    "surface": {
        "method": "plot_surface", "coords": ("x", "y", "z"), "data_kw": {},
        "style": CMAP_STYLE | {"color", "edgecolor", "lw", "linewidth", "rcount", "ccount",
                               "antialiased", "shade"},
        "colorbar": True, "fixed": {"axlim_clip": True}, "proj": "3d", "words": "3-D surface",
    },
    "wireframe": {
        "method": "plot_wireframe", "coords": ("x", "y", "z"), "data_kw": {},
        "style": {"color", "lw", "linewidth", "ls", "linestyle", "alpha", "rcount", "ccount"},
        "fixed": {"axlim_clip": True}, "proj": "3d", "words": "3-D mesh",
    },
    "trisurf": {
        "method": "plot_trisurf", "coords": ("x", "y", "z"), "data_kw": {},
        "style": CMAP_STYLE | {"color", "edgecolor", "lw", "linewidth", "antialiased", "shade"},
        "colorbar": True, "fixed": {"axlim_clip": True}, "proj": "3d", "words": "3-D surface from points",
    },
    "line3d": {
        "method": "plot3D", "coords": ("x", "y", "z"), "data_kw": {},
        "style": LINE_STYLE - {"drawstyle"}, "fixed": {"axlim_clip": True}, "proj": "3d", "words": "3-D line",
    },
    "scatter3d": {
        "method": "scatter3D", "coords": ("x", "y", "z"), "data_kw": {"c": "c", "s": "sizes"},
        "style": CMAP_STYLE | {"color", "s", "marker", "edgecolors", "linewidths", "depthshade"},
        "colorbar": True, "fixed": {"axlim_clip": True}, "proj": "3d", "words": "3-D dots",
    },
}

PROJECTIONS = ("3d", "polar")


def projection(panel):
    """"3d", "polar" or None (an ordinary flat plot)."""
    p = panel.get("projection")
    return p if p in PROJECTIONS else None


def fits(series, panel):
    """Can this curve be drawn on this panel? 3-D kinds need a 3-D panel, and
    a 3-D panel draws only 3-D kinds."""
    need = info(series).get("proj")
    return need == projection(panel) if (need or projection(panel) == "3d") else True

BY_METHOD = {k["method"]: name for name, k in KINDS.items()}

# Kinds whose label goes in the legend (heatmaps and contours use a colour
# bar instead, so their name isn't written into the code).
LEGEND_KINDS = {"line", "step", "errorbar", "band", "bar", "scatter", "box", "hist", "quiver",
                "surface", "wireframe", "trisurf", "line3d", "scatter3d"}

# Kinds that take no gid= or label=: codegen keeps the result as _b and sets
# the gid on the first body / box afterwards.
CONTAINER_KINDS = {"box": "boxes", "violin": "bodies"}

# Every style key any kind draws -- what codesync treats as "spelled out in
# code" (plus the editor-level keys translated by "out").
ALL_STYLE = set().union(*(k["style"] for k in KINDS.values())) | {"color", "alpha", "edgecolor", "lw"}

# Kinds whose z is a 2-D table of x by y (heatmaps, contours, surfaces).
GRID_KINDS = {"heatmap", "contour", "contourf", "surface", "wireframe"}


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


def log_levels(series):
    """Contours on a log colour scale: how many bands, spread evenly in log
    (matplotlib would space them evenly in plain numbers, so a map spanning
    decades came out nearly one colour). None when not needed: not a
    contour, not log, or the levels are given as a list."""
    if kind_of(series) not in ("contour", "contourf"):
        return None
    st = series.get("style") or {}
    if st.get("norm") != "log" or isinstance(st.get("levels"), (list, tuple)):
        return None
    return int(st.get("levels") or 12)


def geomspace_levels(z, vmin, vmax, n):
    """n log-even bands over vmin..vmax (or the positive values of z)."""
    z = np.asarray(z, dtype=float)
    z = z[np.isfinite(z) & (z > 0)]
    lo = vmin if vmin and vmin > 0 else (z.min() if z.size else 1.0)
    hi = vmax if vmax and vmax > 0 else (z.max() if z.size else 10.0)
    if not hi > lo:
        hi = lo * 10
    return np.geomspace(lo, hi, n + 1)


def colorbar_kw(series, three_d):
    """Extra fig.colorbar arguments: log ticks ("log": LogLocator) for
    log-even bands; a 3-D plot's bar sits a little further out, clear of
    the z numbers."""
    kw = {"ticks": "log"} if log_levels(series) else {}
    if three_d:
        kw.update(shrink=0.7, pad=0.18)
    return kw


VIEW_ELEV, VIEW_AZIM = 25, -60     # matplotlib's own 3-D view, a little lower


def full_circle(xlim):
    """A polar plot's angle range that goes all the way round is left to
    matplotlib: setting 0..6.283 makes it a slice with its numbers squashed
    along one edge."""
    return abs(xlim[1] - xlim[0]) >= 2 * np.pi * 0.999


def style_from_kwargs(kind, kwargs):
    """codesync: matplotlib kwargs read from code -> the spec's style."""
    k = KINDS[kind]
    st = {key: v for key, v in kwargs.items() if key in k["style"]}
    return k["back"](st) if "back" in k else st
