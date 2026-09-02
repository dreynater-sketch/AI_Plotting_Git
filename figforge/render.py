"""SPEC -> matplotlib -> SVG (with gids) + geometry map.

Two outputs matter:

  svg       -- matplotlib's own SVG. Every editable text artist carries a gid,
               so the browser can find it as <g id="t_<text_id>">. matplotlib
               draws the text's bbox patch *inside* that group, so translating
               the group moves the white box with the glyphs.

  geometry  -- what the browser needs to invert a pixel drag back into the
               element's native coordinate system (spec section 3.3). For each
               panel: its drawn box in SVG units plus the axis limits/scales.

matplotlib's SVG backend draws at dpi 72, so 1 display pixel == 1 SVG user
unit == 1 point. Display coords are y-up from the bottom; SVG is y-down from
the top, hence the (H - y) flips below.
"""

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch

SVG_DPI = 72

# Style keys we forward to Axes.plot. Anything else in a spec style dict is
# ignored rather than blindly splatted into matplotlib.
STYLE_KEYS = {"color", "lw", "linewidth", "ls", "linestyle", "marker", "ms",
              "markersize", "mew", "markeredgewidth", "mfc", "markerfacecolor",
              "mec", "markeredgecolor", "alpha", "zorder"}


def _resolve(ref, arrays):
    """A series coordinate is either an npz key or an inline literal list."""
    if isinstance(ref, str):
        return arrays[ref]
    return np.asarray(ref, dtype=float)


def _style(style):
    return {k: v for k, v in (style or {}).items() if k in STYLE_KEYS}


def build_figure(spec, arrays):
    """Realise a SPEC as a matplotlib Figure. Returns (fig, axes_by_id)."""
    plt.rcParams.update(matplotlib.rcParamsDefault)
    plt.rcParams.update(spec.get("rcparams", {}))

    panels = spec["panels"]
    fig, axes = plt.subplots(1, len(panels), figsize=tuple(spec["size_in"]))
    if len(panels) == 1:
        axes = [axes]
    fig.set_dpi(SVG_DPI)
    fig.patch.set_facecolor("white")

    sup = spec.get("suptitle")
    if sup and sup.get("text"):
        st = fig.suptitle(sup["text"], fontsize=sup.get("size", 16),
                          y=sup.get("y", 0.98), color=sup.get("color", "black"))
        st.set_gid("t_suptitle")

    axes_by_id = {}
    for ax, p in zip(axes, panels):
        axes_by_id[p["id"]] = ax
        _draw_panel(ax, p, arrays)

    layout = spec.get("layout", {})
    if layout.get("tight", True):
        fig.tight_layout(rect=layout.get("rect", [0, 0, 1, 1]))
    return fig, axes_by_id


def _draw_panel(ax, p, arrays):
    for h in p.get("hlines", []):
        ax.axhline(h["y"], color=h.get("color", "0.8"), ls=h.get("ls", "-"),
                   lw=h.get("lw", 1.0))
    for v in p.get("vlines", []):
        ax.axvline(v["x"], color=v.get("color", "0.8"), ls=v.get("ls", "-"),
                   lw=v.get("lw", 1.0))
    if p.get("zero_lines"):
        ax.axhline(0, color="0.88", lw=0.7)
        ax.axvline(0, color="0.88", lw=0.7)

    for s in p.get("series", []):
        ax.plot(_resolve(s["x"], arrays), _resolve(s["y"], arrays),
                label=s.get("label"), **_style(s.get("style")))

    for a in p.get("arrows", []):
        ax.add_patch(FancyArrowPatch(
            tuple(a["p0"]), tuple(a["p1"]),
            arrowstyle=a.get("arrowstyle", "->"),
            mutation_scale=a.get("mutation_scale", 12),
            lw=a.get("lw", 1.8),
            color=a.get("color", "black"),
            zorder=a.get("zorder", 6),
        ))

    for t in p.get("texts", []):
        art = ax.text(
            t["xy"][0], t["xy"][1], t["text"],
            fontsize=t.get("size", 12),
            color=t.get("color", "black"),
            ha=t.get("ha", "left"),
            va=t.get("va", "baseline"),
            transform=ax.transData if t.get("coords", "data") == "data"
            else ax.transAxes,
            bbox=t.get("bbox"),
            zorder=t.get("zorder", 10),
        )
        art.set_gid("t_" + t["id"])

    title = p.get("title", {})
    if title.get("text"):
        ta = ax.set_title(title["text"], loc=title.get("loc", "center"),
                          fontsize=title.get("size", 14),
                          color=title.get("color", "black"))
        ta.set_gid("t_" + p["id"] + "__title")
    xl = p.get("xlabel", {})
    if xl.get("text"):
        a = ax.set_xlabel(xl["text"], fontsize=xl.get("size", 13),
                          color=xl.get("color", "black"))
        a.set_gid("t_" + p["id"] + "__xlabel")
    yl = p.get("ylabel", {})
    if yl.get("text"):
        a = ax.set_ylabel(yl["text"], fontsize=yl.get("size", 13),
                          color=yl.get("color", "black"))
        a.set_gid("t_" + p["id"] + "__ylabel")

    ax.set_xscale(p.get("xscale", "linear"))
    ax.set_yscale(p.get("yscale", "linear"))
    ax.set_xlim(*p["xlim"])
    ax.set_ylim(*p["ylim"])
    if p.get("aspect", "auto") == "equal":
        ax.set_aspect("equal", "box")

    lg = p.get("legend")
    if lg and any(s.get("label") for s in p.get("series", [])):
        ax.legend(loc=lg.get("loc", "best"), frameon=lg.get("frameon", False),
                  fontsize=lg.get("size", 10))


def _geometry(fig, axes_by_id, spec):
    """Everything the browser needs to convert dropped pixels back to data."""
    H = fig.get_size_inches()[1] * SVG_DPI
    W = fig.get_size_inches()[0] * SVG_DPI

    panels = {}
    for p in spec["panels"]:
        ax = axes_by_id[p["id"]]
        bb = ax.get_window_extent()
        panels[p["id"]] = {
            # SVG-space box: x, y from top-left, then width/height
            "bbox": _f([bb.x0, H - bb.y1, bb.width, bb.height]),
            "xlim": _f(ax.get_xlim()),
            "ylim": _f(ax.get_ylim()),
            "xscale": ax.get_xscale(),
            "yscale": ax.get_yscale(),
        }

    texts = {}
    for p in spec["panels"]:
        ax = axes_by_id[p["id"]]
        for t in p.get("texts", []):
            x, y = ax.transData.transform(t["xy"])
            texts[t["id"]] = {"panel": p["id"], "anchor": _f([x, H - y])}

    return {"width": float(W), "height": float(H),
            "panels": panels, "texts": texts}


def _f(seq):
    """numpy scalars -> plain floats, so the geometry is JSON-serialisable."""
    return [float(v) for v in seq]


def render(spec, arrays):
    """Returns (svg_string, geometry_dict)."""
    fig, axes_by_id = build_figure(spec, arrays)
    # Draw once so tight_layout, aspect-equal box adjustment and text layout
    # have all settled before we read any geometry.
    fig.canvas.draw()
    geom = _geometry(fig, axes_by_id, spec)

    buf = io.StringIO()
    fig.savefig(buf, format="svg", facecolor="white")
    plt.close(fig)
    return buf.getvalue(), geom


def render_png(spec, arrays, path, dpi=None):
    fig, _ = build_figure(spec, arrays)
    fig.savefig(path, dpi=dpi or spec.get("dpi", 200), facecolor="white")
    plt.close(fig)
    return path
