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
    if log:
        vals = vals[vals > 0]
        if not vals.size:
            raise CSVError("log scale needs values above zero")
        lo, hi = float(vals.min()), float(vals.max())
        return [lo / 1.3, hi * 1.3]
    lo, hi = float(vals.min()), float(vals.max())
    if lo == hi:
        pad = abs(lo) * 0.1 or 1.0
        return [lo - pad, hi + pad]
    pad = (hi - lo) * 0.05
    return [round(lo - pad, 12), round(hi + pad, 12)]


MAX_PANELS = 4
SCALES = ("linear", "log")


def build(project, text, x, ys, kind="line", filename=""):
    """-> (spec, arrays). x: a column index; ys: column indices to plot.
    The one-panel figure the New-from-CSV dialog makes."""
    spec, arrays = build_panels(project, text, [{"x": x, "ys": ys, "kind": kind}], filename)
    # Rebuild reads x / ys / plot for a dialog-made figure.
    spec["source"] = {"kind": "csv", "file": "data/source.csv", "x": x,
                      "ys": spec["source"]["panels"][0]["ys"], "plot": kind, "filename": filename}
    return spec, arrays


def build_panels(project, text, panels, filename="", figure_title=""):
    """-> (spec, arrays) for 1-4 panels side by side. Each panel is a dict:
    x, ys (column indices), and optionally kind, title, xlabel, ylabel,
    xscale, yscale -- None or missing picks the default (column names as
    labels, the file name as the first panel's title)."""
    names, cols, _ = parse(text)
    if not isinstance(panels, list) or not 1 <= len(panels) <= MAX_PANELS:
        raise CSVError(f"a figure has 1 to {MAX_PANELS} panels")
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename or project)
    arrays, out, source = {}, [], []
    colour = 0
    for i, pn in enumerate(panels):
        pid = "abcd"[i]
        x, kind = pn.get("x"), pn.get("kind") or "line"
        where = f"panel ({pid}): " if len(panels) > 1 else ""
        if kind not in PLOT_KINDS:
            raise CSVError(f"{where}unknown plot type '{kind}'")
        if not isinstance(x, int) or not 0 <= x < len(cols) or cols[x] is None:
            raise CSVError(f"{where}pick a numeric column for x")
        ys = [y for y in pn.get("ys") or [] if isinstance(y, int) and 0 <= y < len(cols) and y != x]
        if not ys:
            raise CSVError(f"{where}pick at least one numeric column to plot")
        if any(cols[y] is None for y in ys):
            raise CSVError(f"{where}only numeric columns can be plotted")
        xscale, yscale = pn.get("xscale") or "linear", pn.get("yscale") or "linear"
        if xscale not in SCALES or yscale not in SCALES:
            raise CSVError(f"{where}scale must be linear or log")

        # Panel (a) keeps the plain "x" / "y0" names older projects use.
        pre = "" if i == 0 else f"{pid}_"
        arrays[pre + "x"] = cols[x]
        series = []
        for n, y in enumerate(ys):
            key = f"{pre}y{n}"
            arrays[key] = cols[y]
            style = {"color": PALETTE[colour % len(PALETTE)]}
            colour += 1
            if kind == "line":
                style.update(lw=1.6)
            elif kind == "scatter":
                style.update(ls="none", marker="o", ms=4)
            else:
                style.update(lw=1.4, marker="o", ms=3.5)
            series.append({"id": f"{pid}_s{n}", "x": pre + "x", "y": key,
                           "label": _plain(names[y]), "style": style})

        def text_or(value, default):
            return default if value is None else str(value)

        title = text_or(pn.get("title"), _plain(stem) if i == 0 else "")
        ylabel = text_or(pn.get("ylabel"), _plain(names[ys[0]]) if len(ys) == 1 else "value")
        out.append({
            "id": pid,
            "title": {"text": title, "loc": "left", "size": 14, "color": "#000000"},
            "xlabel": {"text": text_or(pn.get("xlabel"), _plain(names[x])), "size": 13, "color": "#000000"},
            "ylabel": {"text": ylabel, "size": 13, "color": "#000000"},
            "xlim": _limits([cols[x]], xscale == "log"),
            "ylim": _limits([cols[y] for y in ys], yscale == "log"),
            "xscale": xscale, "yscale": yscale, "aspect": "auto",
            "xtick_size": 11, "ytick_size": 11, "frame_lw": 0.8,
            "legend": {"loc": "best", "frameon": False, "size": 10, "xy": None} if len(ys) > 1 else None,
            "series": series,
            "arrows": [],
            "texts": [],
        })
        source.append({"x": x, "ys": ys, "kind": kind, "title": pn.get("title"),
                       "xlabel": pn.get("xlabel"), "ylabel": pn.get("ylabel"),
                       "xscale": xscale, "yscale": yscale})

    n = len(out)
    spec = {
        "figure_id": project,
        "rev": 1,
        "size_in": [8.0, 5.0] if n == 1 else [min(4.6 * n, 18.0), 4.6],
        "dpi": 200,
        "data_ref": "data/curves.npz",
        # How to rebuild this figure from its raw data (Start over button).
        "source": {"kind": "csv-panels", "file": "data/source.csv", "panels": source,
                   "filename": filename, "figure_title": figure_title or ""},
        "suptitle": {"text": figure_title or "", "size": 16, "y": 0.98, "color": "#000000"},
        "layout": {"tight": True, "rect": [0, 0, 1, 1]},
        "rcparams": {"font.size": 12},
        "derived": {},
        "panels": out,
    }
    return spec, arrays
