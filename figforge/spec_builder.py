"""Build the initial SPEC for the Q-circle figure.

The SPEC is the semantic layer described in figforge_spec.md section 4: a JSON
document where every editable thing has a stable id. This module produces the
SPEC that reproduces qcircle_C_R_Goff.py; from there the editor (and later, the
AI) only ever mutates the SPEC.

Series x/y are either a string (key into curves.npz) or an inline list of
literal values for one-off marker points.
"""

ORANGE = "#E07A3D"
BLUE = "#4C78A8"
GREEN = "#2E8B57"
NAVY = "#1f4e8c"
GREY15 = "#262626"
GREY20 = "#333333"


def _box(ec, lw, pad=0.25):
    return {"boxstyle": f"round,pad={pad}", "fc": "white", "ec": ec, "lw": lw}


def _text(tid, text, xy, size, color="#000000", bbox=None, ha="left", va="baseline"):
    t = {
        "id": tid,
        "text": text,
        "xy": list(xy),
        "coords": "data",
        "size": size,
        "color": color,
        "ha": ha,
        "va": va,
    }
    if bbox:
        t["bbox"] = bbox
    return t


def build_spec(d):
    """d is the `derived` dict from analyze.analyze()."""

    # ---------------------------------------------------------- panel (a)
    panel_a = {
        "id": "a",
        "title": {"text": r"(a) drive port — $S_{11}$", "loc": "left",
                  "size": 14, "color": "#000000"},
        "xlabel": {"text": r"Re $S_{11}$", "size": 13, "color": "#000000"},
        "ylabel": {"text": r"Im $S_{11}$", "size": 13, "color": "#000000"},
        "xlim": [-0.26, 0.05],
        "ylim": [-0.12, 0.13],
        "xscale": "linear",
        "yscale": "linear",
        "aspect": "equal",
        "xtick_size": 11,
        "ytick_size": 11,
        "frame_lw": 0.8,
        "zero_lines": True,
        "legend": {"loc": "lower right", "frameon": False, "size": 10},
        "series": [
            {"id": "a_meas", "x": "S11_re", "y": "S11_im", "label": "measured",
             "style": {"ls": "none", "marker": ".", "color": BLUE, "ms": 3.0,
                       "alpha": 0.75}},
            {"id": "a_fit", "x": "fit11_re", "y": "fit11_im", "label": "circle fit",
             "style": {"color": ORANGE, "lw": 2.2}},
            {"id": "a_origin", "x": [0.0], "y": [0.0],
             "style": {"ls": "none", "marker": "+", "color": "black", "ms": 16,
                       "mew": 2.0, "zorder": 7}},
            {"id": "a_center", "x": [d["c11x"]], "y": [d["c11y"]],
             "style": {"ls": "none", "marker": "o", "color": "black", "ms": 6,
                       "zorder": 7}},
            {"id": "a_near", "x": [d["n11x"]], "y": [d["n11y"]],
             "style": {"ls": "none", "marker": "s", "color": GREEN, "ms": 7,
                       "zorder": 8}},
            {"id": "a_cline", "x": [0.0, d["c11x"]], "y": [0.0, d["c11y"]],
             "style": {"color": "black", "ls": "--", "lw": 1.2}},
        ],
        "arrows": [
            {"id": "a_R", "p0": [d["c11x"], d["c11y"]], "p1": [d["n11x"], d["n11y"]],
             "arrowstyle": "<->", "color": GREEN, "lw": 1.8,
             "mutation_scale": 12, "zorder": 6},
            {"id": "a_Goff", "p0": _perp(d, 0.018, 0.0, 0.0),
             "p1": _perp(d, 0.018, d["f11x"], d["f11y"]),
             "arrowstyle": "->", "color": NAVY, "lw": 1.8,
             "mutation_scale": 12, "zorder": 6},
        ],
        "texts": [
            _text("a_beta1", rf"$\beta_1={d['b11']:.2f}$", (-0.248, 0.108), 12,
                  bbox=_box(ORANGE, 1.3)),
            _text("a_R", rf"$R={d['R11']:.3f}$", (-0.205, 0.055), 11, GREEN),
            _text("a_C", rf"$|C|={d['C11']:.2f}$", (-0.095, 0.055), 11, GREY15),
            _text("a_origin_lbl", "origin", (0.005, 0.012), 10, GREY20),
            _text("a_goff",
                  rf"$|\Gamma_\mathrm{{off}}|=|C|+R={d['G11']:.2f}$",
                  (-0.12, -0.038), 10, NAVY, bbox=_box(NAVY, 1.0, pad=0.2)),
            _text("a_dbeta",
                  rf"$d=2R/|\Gamma_\mathrm{{off}}|={2 * d['R11']:.2f}/{d['G11']:.2f}$"
                  "\n"
                  rf"$\beta=d/(2-d)={d['b11']:.2f}$",
                  (-0.248, -0.108), 10, bbox=_box("#999999", 0.8)),
        ],
    }

    # ---------------------------------------------------------- panel (b)
    off = 0.012
    panel_b = {
        "id": "b",
        "title": {"text": r"(b) pickup port — $S_{33}$", "loc": "left",
                  "size": 14, "color": "#000000"},
        "xlabel": {"text": r"Re $S_{33}$", "size": 13, "color": "#000000"},
        "ylabel": {"text": r"Im $S_{33}$", "size": 13, "color": "#000000"},
        "xlim": [-0.16, 0.14],
        "ylim": [-0.31, 0.04],
        "xscale": "linear",
        "yscale": "linear",
        "aspect": "equal",
        "xtick_size": 11,
        "ytick_size": 11,
        "frame_lw": 0.8,
        "zero_lines": True,
        "legend": {"loc": "lower right", "frameon": False, "size": 10},
        "series": [
            {"id": "b_meas", "x": "S33_re", "y": "S33_im", "label": "measured",
             "style": {"ls": "none", "marker": ".", "color": BLUE, "ms": 3.0,
                       "alpha": 0.75}},
            {"id": "b_fit", "x": "fit33_re", "y": "fit33_im", "label": "circle fit",
             "style": {"color": ORANGE, "lw": 2.2}},
            {"id": "b_origin", "x": [0.0], "y": [0.0],
             "style": {"ls": "none", "marker": "+", "color": "black", "ms": 16,
                       "mew": 2.0, "zorder": 7}},
            {"id": "b_center", "x": [d["c33x"]], "y": [d["c33y"]],
             "style": {"ls": "none", "marker": "o", "color": "black", "ms": 6,
                       "zorder": 7}},
            {"id": "b_near", "x": [d["n33x"]], "y": [d["n33y"]],
             "style": {"ls": "none", "marker": "s", "color": GREEN, "ms": 7,
                       "zorder": 8}},
            {"id": "b_cline", "x": [0.0, d["c33x"]], "y": [0.0, d["c33y"]],
             "style": {"color": "black", "ls": "--", "lw": 1.2}},
        ],
        "arrows": [
            {"id": "b_R", "p0": [d["c33x"], d["c33y"]], "p1": [d["n33x"], d["n33y"]],
             "arrowstyle": "<->", "color": GREEN, "lw": 1.8,
             "mutation_scale": 12, "zorder": 6},
            {"id": "b_Goff", "p0": [-off, 0.0], "p1": [d["f33x"] - off, d["f33y"]],
             "arrowstyle": "<->", "color": NAVY, "lw": 1.8,
             "mutation_scale": 12, "zorder": 6},
        ],
        "texts": [
            _text("b_beta2", rf"$\beta_2={d['b33']:.2f}$", (-0.148, 0.018), 12,
                  bbox=_box(ORANGE, 1.3)),
            _text("b_origin_lbl", "origin", (0.018, 0.010), 10, GREY20),
            _text("b_C", rf"$|C|={d['C33']:.2f}$", (0.018, -0.10), 11, GREY15),
            _text("b_R", rf"$R={d['R33']:.3f}$", (0.018, -0.175), 11, GREEN),
            _text("b_goff",
                  rf"$|\Gamma_\mathrm{{off}}|$" "\n" rf"$={d['G33']:.2f}$",
                  (-0.148, -0.145), 10, NAVY, bbox=_box(NAVY, 1.0, pad=0.2)),
            _text("b_dbeta",
                  rf"$d=2R/|\Gamma_\mathrm{{off}}|={2 * d['R33']:.2f}/{d['G33']:.2f}$"
                  "\n"
                  rf"$\beta=d/(2-d)={d['b33']:.2f}$",
                  (-0.148, -0.292), 10, bbox=_box("#999999", 0.8)),
        ],
    }

    # ---------------------------------------------------------- panel (c)
    d1, d2 = d["df1_kHz"], d["df2_kHz"]
    panel_c = {
        "id": "c",
        "title": {"text": r"(c) transmission — $S_{31}$", "loc": "left",
                  "size": 14, "color": "#000000"},
        "xlabel": {"text": "frequency offset (kHz)", "size": 13, "color": "#000000"},
        "ylabel": {"text": r"$|S_{31}|^2$ (norm. to peak)", "size": 13,
                   "color": "#000000"},
        "xlim": [-40, 40],
        "ylim": [-0.06, 1.16],
        "xscale": "linear",
        "yscale": "linear",
        "aspect": "auto",
        "xtick_size": 11,
        "ytick_size": 11,
        "frame_lw": 0.8,
        "zero_lines": False,
        "legend": {"loc": "lower right", "frameon": False, "size": 10},
        "series": [
            {"id": "c_meas", "x": "df_kHz", "y": "P_norm", "label": "measured",
             "style": {"ls": "none", "marker": ".", "color": BLUE, "ms": 3.0}},
            {"id": "c_fit", "x": "df_kHz", "y": "lorentzian",
             "label": "Lorentzian fit", "style": {"color": ORANGE, "lw": 2.2}},
            {"id": "c_halfline", "x": [d1, d2], "y": [0.5, 0.5],
             "style": {"color": ORANGE, "lw": 1.6}},
            {"id": "c_m1", "x": [d1], "y": [0.5],
             "style": {"ls": "none", "marker": "D", "color": ORANGE, "ms": 5}},
            {"id": "c_m2", "x": [d2], "y": [0.5],
             "style": {"ls": "none", "marker": "D", "color": ORANGE, "ms": 5}},
            {"id": "c_peak", "x": [0.0], "y": [1.0],
             "style": {"ls": "none", "marker": "o", "color": ORANGE, "ms": 7}},
        ],
        "vlines": [
            {"id": "c_v1", "x": d1, "color": ORANGE, "ls": ":", "lw": 1.1},
            {"id": "c_v0", "x": 0.0, "color": ORANGE, "ls": ":", "lw": 1.1},
            {"id": "c_v2", "x": d2, "color": ORANGE, "ls": ":", "lw": 1.1},
        ],
        "hlines": [
            {"id": "c_h50", "y": 0.5, "color": "#cccccc", "ls": ":", "lw": 0.8},
        ],
        "arrows": [
            {"id": "c_df", "p0": [d1, 0.5], "p1": [d2, 0.5], "arrowstyle": "<->",
             "color": ORANGE, "lw": 1.6, "mutation_scale": 10, "zorder": 6},
        ],
        "texts": [
            _text("c_deltaf", r"$\Delta f$", (1.6, 0.56), 12, ORANGE),
            _text("c_f1", r"$f_1$", (d1, 1.08), 12, ORANGE, ha="center"),
            _text("c_f0", r"$f_0$", (0.0, 1.08), 12, ORANGE, ha="center"),
            _text("c_f2", r"$f_2$", (d2, 1.08), 12, ORANGE, ha="center"),
            _text("c_QL", rf"$Q_L={d['QL'] / 1e3:.0f}\,\mathrm{{k}}$",
                  (-38, 1.02), 12, bbox=_box(ORANGE, 1.3)),
            _text("c_Q0",
                  r"$Q_0=Q_L(1+\beta_1+\beta_2)$"
                  "\n"
                  rf"$={d['QL'] / 1e3:.0f}\mathrm{{k}}\times"
                  rf"{1 + d['b11'] + d['b33']:.2f}={d['Q0'] / 1e6:.2f}\mathrm{{M}}$",
                  (-38, 0.08), 10, bbox=_box("#999999", 0.8)),
        ],
    }

    return {
        "figure_id": "qcircle",
        "rev": 1,
        "size_in": [16.2, 5.35],
        "dpi": 170,
        "data_ref": "data/curves.npz",
        "suptitle": {"text": r"$2\,\mathrm{K}$ Nb-plated Cu cavity",
                     "size": 18, "y": 0.98, "color": "#000000"},
        "layout": {"tight": True, "rect": [0, 0, 1, 0.96]},
        "rcparams": {
            "font.size": 12,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
        },
        "derived": d,
        "panels": [panel_a, panel_b, panel_c],
    }


def _perp(d, off, px, py):
    """Point (px,py) shifted perpendicular to the origin->center11 direction.

    Reproduces the `off` nudge in the original panel (a), which pushes the
    |Goff| arrow off the dashed |C| line so the two don't overlap.
    """
    ux, uy = d["c11x"] / d["C11"], d["c11y"] / d["C11"]
    return [px - uy * off, py + ux * off]
