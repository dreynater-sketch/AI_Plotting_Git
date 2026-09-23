"""Any CSV -> a new FigForge project: pick an x column and y columns.

The general-purpose way in (the Q-circle figure is the special case with its
own analysis in analyze.py / spec_builder.py). inspect() reads an uploaded
file and describes its columns so the page can offer choices; build() turns
the choices into a one-panel spec plus the curve arrays figure.py loads.

Tolerant of what real exported data looks like: comma / semicolon / tab /
whitespace separated, an optional header row, '#' comment lines, blank
lines, and non-numeric cells (read as missing, so a curve just breaks there).
"""

import csv
import math
import re

import numpy as np

MAX_BYTES = 4 * 1024 * 1024   # Vercel caps a request body at 4.5 MB
MAX_COLUMNS = 200
PALETTE = ["#4C78A8", "#E07A3D", "#2E8B57", "#B03A6F", "#6F4FB0",
           "#8C6D31", "#1F9AA8", "#D62728", "#7F7F7F", "#BCBD22"]
PLOT_KINDS = ("line", "scatter", "line+markers")


class CSVError(ValueError):
    pass


def _plain(text):
    """Column names shown as labels: a lone '$' would start matplotlib mathtext."""
    return text.replace("$", r"\$")


def _number(cell):
    cell = cell.strip()
    if not cell:
        return math.nan
    try:
        return float(cell)
    except ValueError:
        # "1,5" style decimals from European exports
        if re.fullmatch(r"[-+]?\d+,\d+(e[-+]?\d+)?", cell, re.I):
            return float(cell.replace(",", "."))
        return None


def _rows(text):
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        raise CSVError("the file has no data rows")
    sample = "\n".join(lines[:50])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = None  # whitespace-separated
    if delim is None:
        rows = [l.split() for l in lines]
    else:
        rows = list(csv.reader(lines, delimiter=delim))
    return rows, {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe", None: "spaces"}[delim]


def parse(text):
    """-> (column names, list of float arrays, separator name). Columns that
    hold no numbers at all come back as None."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise CSVError("that file is over 4 MB")
    rows, sep = _rows(text)
    width = max(len(r) for r in rows)
    if width > MAX_COLUMNS:
        raise CSVError(f"that file has {width} columns; the limit is {MAX_COLUMNS}")
    first = rows[0]
    header = any(_number(c) is None for c in first)
    names = [c.strip() or f"column {i + 1}" for i, c in enumerate(first)] if header else []
    names += [f"column {i + 1}" for i in range(len(names), width)]
    body = rows[1:] if header else rows
    if not body:
        raise CSVError("the file has a header but no data rows")
    cols = []
    for j in range(width):
        vals = []
        numeric = 0
        for r in body:
            v = _number(r[j]) if j < len(r) else math.nan
            if v is None:
                v = math.nan
            elif not math.isnan(v):
                numeric += 1
            vals.append(v)
        cols.append(np.array(vals, dtype=float) if numeric else None)
    return names, cols, sep


def inspect(text):
    names, cols, sep = parse(text)
    out = []
    for i, (name, col) in enumerate(zip(names, cols)):
        entry = {"index": i, "name": name, "numeric": col is not None}
        if col is not None:
            good = col[~np.isnan(col)]
            entry.update(count=int(good.size), min=float(good.min()), max=float(good.max()))
        out.append(entry)
    if not any(c["numeric"] for c in out):
        raise CSVError("no column in that file holds numbers")
    rows = next(len(c) for c in cols if c is not None)
    return {"columns": out, "rows": rows, "separator": sep}


def _limits(arrays, log=False):
    vals = np.concatenate([a[~np.isnan(a)] for a in arrays])
    lo, hi = float(vals.min()), float(vals.max())
    if lo == hi:
        pad = abs(lo) * 0.1 or 1.0
        return [lo - pad, hi + pad]
    pad = (hi - lo) * 0.05
    return [round(lo - pad, 12), round(hi + pad, 12)]


def build(project, text, x, ys, kind="line", filename=""):
    """-> (spec, arrays). x: a column index; ys: column indices to plot."""
    names, cols, _ = parse(text)
    if kind not in PLOT_KINDS:
        raise CSVError("unknown plot type")
    if not isinstance(x, int) or not 0 <= x < len(cols) or cols[x] is None:
        raise CSVError("pick a numeric column for x")
    ys = [y for y in ys if isinstance(y, int) and 0 <= y < len(cols) and y != x]
    if not ys:
        raise CSVError("pick at least one numeric column to plot")
    if any(cols[y] is None for y in ys):
        raise CSVError("only numeric columns can be plotted")

    arrays = {"x": cols[x]}
    series = []
    for n, y in enumerate(ys):
        key = f"y{n}"
        arrays[key] = cols[y]
        colour = PALETTE[n % len(PALETTE)]
        style = {"color": colour}
        if kind == "line":
            style.update(lw=1.6)
        elif kind == "scatter":
            style.update(ls="none", marker="o", ms=4)
        else:
            style.update(lw=1.4, marker="o", ms=3.5)
        series.append({"id": f"a_s{n}", "x": "x", "y": key, "label": _plain(names[y]), "style": style})

    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename or project)
    panel = {
        "id": "a",
        "title": {"text": _plain(stem), "loc": "left", "size": 14, "color": "#000000"},
        "xlabel": {"text": _plain(names[x]), "size": 13, "color": "#000000"},
        "ylabel": {"text": _plain(names[ys[0]]) if len(ys) == 1 else "value", "size": 13, "color": "#000000"},
        "xlim": _limits([cols[x]]),
        "ylim": _limits([cols[y] for y in ys]),
        "xscale": "linear", "yscale": "linear", "aspect": "auto",
        "xtick_size": 11, "ytick_size": 11, "frame_lw": 0.8,
        "legend": {"loc": "best", "frameon": False, "size": 10, "xy": None} if len(ys) > 1 else None,
        "series": series,
        "arrows": [],
        "texts": [],
    }
    spec = {
        "figure_id": project,
        "rev": 1,
        "size_in": [8.0, 5.0],
        "dpi": 200,
        "data_ref": "data/curves.npz",
        # How to rebuild this figure from its raw data (Rebuild button).
        "source": {"kind": "csv", "file": "data/source.csv", "x": x, "ys": ys,
                   "plot": kind, "filename": filename},
        "suptitle": {"text": "", "size": 16, "y": 0.98, "color": "#000000"},
        "layout": {"tight": True, "rect": [0, 0, 1, 1]},
        "rcparams": {"font.size": 12},
        "derived": {},
        "panels": [panel],
    }
    return spec, arrays
