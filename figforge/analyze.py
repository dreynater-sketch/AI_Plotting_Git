"""Q-circle analysis: VNA CSV -> curve arrays + derived scalars.

This is the science layer, lifted verbatim from qcircle_C_R_Goff.py. It knows
nothing about plotting. It produces:

  arrays  -- everything a curve is drawn from, saved to curves.npz
  derived -- the scalars (beta1, beta2, QL, Q0, ...) that get formatted into
             label text

Circle fit in the reflection plane:
    |C|    = distance from origin to circle center
    R      = circle radius
    |Goff| = |C| + R          (far-side / detuned reflection)
    d      = 2R / |Goff|
    beta   = d / (2 - d)
QL from the 3 dB width of |S31|^2, then Q0 = QL (1 + beta1 + beta2).
"""

import numpy as np


def load_csv(path):
    rows = []
    with open(path) as f:
        for line in f:
            if (not line.strip() or line[0] in "!B"
                    or line.startswith("Freq")):
                continue
            p = line.strip().split(",")
            if len(p) < 7:
                continue
            try:
                rows.append([float(x) for x in p[:7]])
            except ValueError:
                pass
    d = np.array(rows)
    freq = d[:, 0]

    def magph(db, deg):
        return 10 ** (db / 20.0) * np.exp(1j * np.deg2rad(deg))

    S31 = magph(d[:, 1], d[:, 2])
    S11 = magph(d[:, 3], d[:, 4])
    S33 = magph(d[:, 5], d[:, 6])
    return freq, S11, S33, S31


def fit_circle(S):
    x, y = S.real, S.imag
    a, b, c = np.linalg.lstsq(
        np.c_[x, y, np.ones_like(x)], -(x ** 2 + y ** 2), rcond=None
    )[0]
    cx, cy = -a / 2.0, -b / 2.0
    R = np.sqrt((a / 2.0) ** 2 + (b / 2.0) ** 2 - c)
    return cx, cy, R


def far_point(cx, cy, R):
    C = np.hypot(cx, cy)
    return cx * (C + R) / C, cy * (C + R) / C


def near_point(cx, cy, R):
    C = np.hypot(cx, cy)
    return cx * (C - R) / C, cy * (C - R) / C


def analyze(csv_path):
    """Run the full extraction. Returns (arrays, derived)."""
    freq, S11, S33, S31 = load_csv(csv_path)

    c11x, c11y, R11 = fit_circle(S11)
    c33x, c33y, R33 = fit_circle(S33)
    C11 = np.hypot(c11x, c11y)
    C33 = np.hypot(c33x, c33y)
    G11 = C11 + R11
    G33 = C33 + R33
    d11 = 2 * R11 / G11
    d33 = 2 * R33 / G33
    b11 = d11 / (2 - d11)
    b33 = d33 / (2 - d33)

    P = np.abs(S31) ** 2
    imax = int(np.argmax(P))
    f0 = freq[imax]
    half = P[imax] / 2.0
    iL = np.where(P[:imax] <= half)[0][-1]
    iR = imax + np.where(P[imax:] <= half)[0][0]
    f1, f2 = freq[iL], freq[iR]
    QL = f0 / (f2 - f1)
    Q0 = QL * (1 + b11 + b33)

    # ---- curve arrays -------------------------------------------------
    th = np.linspace(0, 2 * np.pi, 400)
    df = (freq - f0) / 1e3
    Pn = P / P[imax]
    lor = 1.0 / (1.0 + (2.0 * (freq - f0) / (f2 - f1)) ** 2)

    f11x, f11y = far_point(c11x, c11y, R11)
    n11x, n11y = near_point(c11x, c11y, R11)
    f33x, f33y = far_point(c33x, c33y, R33)
    n33x, n33y = near_point(c33x, c33y, R33)

    arrays = {
        "S11_re": S11.real, "S11_im": S11.imag,
        "S33_re": S33.real, "S33_im": S33.imag,
        "fit11_re": c11x + R11 * np.cos(th), "fit11_im": c11y + R11 * np.sin(th),
        "fit33_re": c33x + R33 * np.cos(th), "fit33_im": c33y + R33 * np.sin(th),
        "df_kHz": df, "P_norm": Pn, "lorentzian": lor,
    }

    derived = {
        "c11x": float(c11x), "c11y": float(c11y), "R11": float(R11),
        "c33x": float(c33x), "c33y": float(c33y), "R33": float(R33),
        "C11": float(C11), "C33": float(C33),
        "G11": float(G11), "G33": float(G33),
        "b11": float(b11), "b33": float(b33),
        "f11x": float(f11x), "f11y": float(f11y),
        "n11x": float(n11x), "n11y": float(n11y),
        "f33x": float(f33x), "f33y": float(f33y),
        "n33x": float(n33x), "n33y": float(n33y),
        "f0": float(f0), "f1": float(f1), "f2": float(f2),
        "df1_kHz": float((f1 - f0) / 1e3), "df2_kHz": float((f2 - f0) / 1e3),
        "QL": float(QL), "Q0": float(Q0),
    }
    return arrays, derived
