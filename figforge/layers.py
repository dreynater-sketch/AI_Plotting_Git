"""The kinds of curve a panel can hold -- one table the renderer, codegen and
codesync all read, so a new kind is one entry here (plus editor fields).

A series in the spec is {"id", "kind"?, "label"?, "style", <coords>}: each
coordinate is an npz key (or an inline list). "kind" is omitted for plain
lines, so figures made before kinds existed are unchanged.

    method   the Axes method that draws it (and that codesync recognises)
    coords   positional data arguments, in order, as spec fields
    data_kw  keyword data arguments: {matplotlib kwarg: spec field}
    style    style keys passed straight through (anything else in a
             series' style is kept in the spec but not drawn)
"""

LINE_STYLE = {"color", "lw", "linewidth", "ls", "linestyle", "marker", "ms",
              "markersize", "mew", "markeredgewidth", "mfc", "markerfacecolor",
              "mec", "markeredgecolor", "alpha", "zorder", "drawstyle"}

KINDS = {
    "line": {
        "method": "plot", "coords": ("x", "y"), "data_kw": {},
        "style": LINE_STYLE,
        "words": "line",
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
}

BY_METHOD = {k["method"]: name for name, k in KINDS.items()}

# Every style key any kind draws -- what codesync treats as "spelled out in code".
ALL_STYLE = set().union(*(k["style"] for k in KINDS.values()))


def kind_of(series):
    return series.get("kind") or "line"


def info(series):
    return KINDS[kind_of(series)]


def data_fields(series):
    """Every spec field of this series that holds data (coords + data_kw)."""
    k = info(series)
    return list(k["coords"]) + [f for f in k["data_kw"].values() if series.get(f) is not None]


def style_for(series):
    """The style keys this kind actually draws."""
    allowed = info(series)["style"]
    return {key: v for key, v in (series.get("style") or {}).items() if key in allowed}
