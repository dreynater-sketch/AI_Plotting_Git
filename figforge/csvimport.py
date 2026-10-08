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
PLOT_KINDS = ("line", "scatter", "line+markers", "steps")


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
                      "ys": [y for y in ys if y != x], "plot": kind, "filename": filename}
    return spec, arrays


LAYER_KINDS = ("errorbar", "band", "bar", "scatter", "heatmap", "contour", "contourf", "box")
GRID_KINDS = ("heatmap", "contour", "contourf")
MAX_GRID_CELLS = 1_000_000


def _grid(xcol, ycol, zcol):
    """Tidy x, y, z columns -> (xs, ys, Z) with Z[len(ys), len(xs)]; cells the
    table doesn't list stay blank (NaN)."""
    ok = ~(np.isnan(xcol) | np.isnan(ycol))
    xs, ys = np.unique(xcol[ok]), np.unique(ycol[ok])
    if xs.size < 2 or ys.size < 2:
        raise CSVError("a heatmap or contour needs at least 2 different x and 2 different y values")
    if xs.size * ys.size > MAX_GRID_CELLS:
        raise CSVError(f"that grid is {xs.size} x {ys.size}; the limit is {MAX_GRID_CELLS:,} cells")
    Z = np.full((ys.size, xs.size), np.nan)
    Z[np.searchsorted(ys, ycol[ok]), np.searchsorted(xs, xcol[ok])] = zcol[ok]
    return xs, ys, Z


def _edges(v):
    """Outer edges of cells centred on v (what pcolormesh shading='auto' draws)."""
    return float(v[0] - (v[1] - v[0]) / 2), float(v[-1] + (v[-1] - v[-2]) / 2)


def _col(names, cols, ref, what):
    """A numeric column, by header name or index -> its index. Names make a
    saved recipe survive columns being added to the table later."""
    if isinstance(ref, str):
        if ref in names and names.count(ref) == 1:
            i = names.index(ref)
        else:
            norm = lambda s: re.sub(r"\s+", "", str(s)).lower()
            hits = [j for j, n in enumerate(names) if norm(n) == norm(ref)]
            if len(hits) != 1:
                raise CSVError(f"no column named '{ref}' for {what}")
            i = hits[0]
    elif isinstance(ref, int) and not isinstance(ref, bool) and 0 <= ref < len(cols):
        i = ref
    else:
        raise CSVError(f"pick a column for {what}")
    if cols[i] is None:
        raise CSVError(f"column '{names[i]}' ({what}) holds no numbers")
    return i


def _curves(idp, pre, names, cols, batch, arrays, colour):
    """One batch of curves for a panel: batch = {x, ys, kind, layers}; a
    layer may have its own x (a fit on a finer grid than the data).
    -> (behind, front, lim_x, lim_y, colour, has_bar, exact). exact = data
    edges the axes should hug (heatmaps, contours) instead of padding.
    Array keys start with `pre` and ids with `idp`, so batches never collide."""
    layers = batch.get("layers") or []
    only_boxes = layers and all(ly.get("kind") == "box" for ly in layers) and not batch.get("ys")
    x = None if only_boxes and batch.get("x") in (None, "") else _col(names, cols, batch.get("x"), "x")
    kind = batch.get("kind") or "line"
    if kind not in PLOT_KINDS:
        raise CSVError(f"unknown plot type '{kind}'")
    ys = [_col(names, cols, y, "y") for y in batch.get("ys") or []]
    ys = [y for y in ys if y != x]
    if not ys and not layers:
        raise CSVError("pick at least one numeric column to plot")
    xkey = pre + "x"
    behind, front, lim_x, lim_y, has_bar, exact = [], [], [], [], False, []
    if x is not None:
        arrays[xkey] = cols[x]
        if ys or any(ly.get("kind") not in GRID_KINDS + ("box",) for ly in layers):
            lim_x.append(cols[x])
    for n, y in enumerate(ys):
        key = f"{pre}y{n}"
        arrays[key] = cols[y]
        style = {"color": PALETTE[colour % len(PALETTE)]}
        colour += 1
        item = {"id": f"{idp}s{n}", "x": xkey, "y": key, "label": _plain(names[y]), "style": style}
        if kind == "line":
            style.update(lw=1.6)
        elif kind == "scatter":
            style.update(ls="none", marker="o", ms=4)
        elif kind == "steps":
            style.update(lw=1.4, where="mid")
            item["kind"] = "step"
        else:
            style.update(lw=1.4, marker="o", ms=3.5)
        front.append(item)
        lim_y.append(cols[y])
    for m, ly in enumerate(layers):
        lk = ly.get("kind")
        if lk not in LAYER_KINDS:
            raise CSVError(f"unknown layer kind '{lk}'")
        colour_hex = PALETTE[colour % len(PALETTE)]
        colour += 1
        key, ex = f"{pre}l{m}", f"{pre}l{m}b"
        if lk == "box":
            groups = [_col(names, cols, c, "a box plot group") for c in ly.get("columns") or []]
            if not groups:
                raise CSVError("a box plot needs at least one column (one per group)")
            gkeys = []
            for j, g in enumerate(groups):
                gk = f"{pre}l{m}g{j}"
                arrays[gk] = cols[g][~np.isnan(cols[g])]
                gkeys.append(gk)
                lim_y.append(arrays[gk])
            lim_x.append(np.array([0.5, len(groups) + 0.5]))
            front.append({"id": f"{idp}l{m}", "kind": "box", "groups": gkeys,
                          "label": _plain(ly.get("label") or ""),
                          "style": {"color": colour_hex, "alpha": 0.7, "widths": 0.6,
                                    "medianprops": {"color": "black"},
                                    "positions": list(range(1, len(groups) + 1)),
                                    "tick_labels": [_plain(names[g]) for g in groups]}})
            continue
        if x is None:
            raise CSVError(f"a {lk} layer needs an x column")
        y = _col(names, cols, ly.get("y"), f"the {lk} layer")
        lx = xkey
        if ly.get("x") not in (None, "", x):
            xi = _col(names, cols, ly["x"], f"the {lk} layer's x")
            if xi != x:
                lx = f"{pre}l{m}x"
                arrays[lx] = cols[xi]
        if lk in GRID_KINDS:
            z = _col(names, cols, ly.get("extra"), f"the {lk} layer's value (z) column")
            xs, gys, Z = _grid(arrays[lx], cols[y], cols[z])
            gx, gy = f"{pre}l{m}gx", f"{pre}l{m}gy"
            arrays[gx], arrays[gy], arrays[key] = xs, gys, Z
            item = {"id": f"{idp}l{m}", "kind": lk, "x": gx, "y": gy, "z": key,
                    "label": _plain(ly.get("label") or names[z])}
            if lk == "contour":
                # Over a heatmap, white lines; on their own, coloured by level.
                over = any(o.get("kind") in ("heatmap", "contourf") for o in layers)
                item["style"] = ({"levels": 8, "colors": "white", "linewidths": 0.8} if over
                                 else {"levels": 8, "cmap": "viridis", "linewidths": 1.0})
                exact.append((float(xs[0]), float(xs[-1]), float(gys[0]), float(gys[-1])))
            else:
                item["style"] = {"cmap": "viridis"} if lk == "heatmap" else {"cmap": "viridis", "levels": 12}
                item["colorbar"] = {"label": _plain(ly.get("label") or names[z])}
                if lk == "heatmap":
                    exact.append(_edges(xs) + _edges(gys))
                else:
                    exact.append((float(xs[0]), float(xs[-1]), float(gys[0]), float(gys[-1])))
            behind.append(item)
            continue
        extra = (_col(names, cols, ly.get("extra"), f"the {lk} layer's second column")
                 if lk in ("errorbar", "band") or (lk == "scatter" and ly.get("extra") not in (None, ""))
                 else None)
        if lx != xkey:
            lim_x.append(arrays[lx])
        elif x is not None and not lim_x:
            lim_x.append(cols[x])
        arrays[key] = cols[y]
        default = f"{names[y]} – {names[extra]}" if lk == "band" else names[y]
        item = {"id": f"{idp}l{m}",
                "kind": lk, "x": lx, "y": key, "label": _plain(ly.get("label") or default)}
        if lk == "errorbar":
            arrays[ex] = np.abs(cols[extra])
            item.update(yerr=ex, style={"color": colour_hex, "ls": "none", "marker": "o",
                                        "ms": 4, "capsize": 3, "elinewidth": 1})
            lim_y += [cols[y] - arrays[ex], cols[y] + arrays[ex]]
            front.append(item)
        elif lk == "band":
            arrays[ex] = cols[extra]
            item.update(y2=ex, style={"color": colour_hex, "alpha": 0.25, "lw": 0, "zorder": 1})
            lim_y += [cols[y], cols[extra]]
            behind.append(item)
        elif lk == "scatter":
            # Dots coloured by one column (with a colour bar) and, optionally,
            # sized by another; without a colour column, plain coloured dots.
            style = {"s": 25}
            if extra is not None:
                arrays[ex] = cols[extra]
                item["c"] = ex
                style["cmap"] = "viridis"
                item["colorbar"] = {"label": _plain(names[extra])}
            else:
                style["color"] = colour_hex
            if ly.get("size") not in (None, ""):
                sz = _col(names, cols, ly["size"], "the dot size column")
                v = cols[sz]
                lo, hi = np.nanmin(v), np.nanmax(v)
                arrays[f"{pre}l{m}s"] = 10 + 90 * (v - lo) / (hi - lo) if hi > lo else np.full_like(v, 30)
                item["sizes"] = f"{pre}l{m}s"
                style.pop("s")
            item["style"] = style
            lim_y.append(cols[y])
            front.append(item)
        else:  # bar: as wide as 80% of the closest spacing between x values
            xs = np.unique(arrays[lx][~np.isnan(arrays[lx])])
            width = float(np.min(np.diff(xs))) * 0.8 if xs.size > 1 else 0.8
            item["style"] = {"color": colour_hex, "width": round(width, 12), "alpha": 0.85}
            lim_y += [cols[y], np.zeros(1)]   # bars stand on zero
            has_bar = True
            behind.append(item)
    return behind, front, lim_x, lim_y, colour, has_bar, exact


def _names_of(names, cols, batch):
    """The batch with every column reference written as its header name."""
    def nm(ref, what):
        return names[_col(names, cols, ref, what)]
    has_x = batch.get("x") not in (None, "")
    out = {"x": nm(batch["x"], "x") if has_x else "", "ys": [nm(y, "y") for y in batch.get("ys") or []],
           "kind": batch.get("kind") or "line", "layers": []}
    for ly in batch.get("layers") or []:
        l = {"kind": ly.get("kind"), "label": ly.get("label") or ""}
        if ly.get("kind") == "box":
            l["columns"] = [nm(c, "group") for c in ly.get("columns") or []]
        else:
            l["y"] = nm(ly.get("y"), "layer")
            if ly.get("extra") not in (None, ""):
                l["extra"] = nm(ly["extra"], "layer")
            if ly.get("size") not in (None, ""):
                l["size"] = nm(ly["size"], "dot size")
        if ly.get("x") not in (None, ""):
            l["x"] = nm(ly["x"], "layer x")
        out["layers"].append(l)
    return out


def build_panels(project, text, panels, filename="", figure_title=""):
    """-> (spec, arrays) for 1-4 panels side by side. Each panel is a dict:
    x, ys, kind, layers (columns by index or header name), and optionally
    title, xlabel, ylabel, xscale, yscale -- None or missing picks the
    default (column names as labels, the file name as the first panel's
    title) -- plus "more": later batches of {x, ys, kind, layers} added with
    add_batch(). A layer is {kind: errorbar|band|bar, y, extra, label, x?}:
    extra is the error column (errorbar) or the upper edge (band); x, if
    given, is the layer's own x column."""
    names, cols, _ = parse(text)
    if not isinstance(panels, list) or not 1 <= len(panels) <= MAX_PANELS:
        raise CSVError(f"a figure has 1 to {MAX_PANELS} panels")
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename or project)
    arrays, out, source = {}, [], []
    colour = 0
    for i, pn in enumerate(panels):
        pid = "abcd"[i]
        where = f"panel ({pid}): " if len(panels) > 1 else ""
        xscale, yscale = pn.get("xscale") or "linear", pn.get("yscale") or "linear"
        if xscale not in SCALES or yscale not in SCALES:
            raise CSVError(f"{where}scale must be linear or log")
        batches = [pn] + list(pn.get("more") or [])
        behind, front, lim_x, lim_y, has_bar, exact = [], [], [], [], False, []
        try:
            for b, batch in enumerate(batches):
                # Panel (a)'s first batch keeps the plain "x" / "y0" names
                # older projects use.
                pre = ("" if i == 0 else f"{pid}_") if b == 0 else f"{pid}_m{b}_"
                idp = f"{pid}_" if b == 0 else f"{pid}_m{b}_"
                bh, fr, lx, ly_, colour, bar, ex_ = _curves(idp, pre, names, cols, batch, arrays, colour)
                behind += bh; front += fr; lim_x += lx; lim_y += ly_; has_bar |= bar; exact += ex_
            recipe = [_names_of(names, cols, batch) for batch in batches]
        except CSVError as e:
            raise CSVError(f"{where}{e}")

        def text_or(value, default):
            return default if value is None else str(value)

        # A heatmap or contour fills the box exactly; other curves get padding.
        ex_x = ex_y = None
        if exact:
            ex_x = [min(e[0] for e in exact), max(e[1] for e in exact)]
            ex_y = [min(e[2] for e in exact), max(e[3] for e in exact)]
        xlim = _limits(lim_x, xscale == "log") if lim_x else ex_x
        ylim = _limits(lim_y, yscale == "log") if lim_y else ex_y
        if exact:
            xlim = [min(xlim[0], ex_x[0]), max(xlim[1], ex_x[1])] if lim_x else ex_x
            ylim = [min(ylim[0], ex_y[0]), max(ylim[1], ex_y[1])] if lim_y else ex_y
        # Bars stand on zero: no padding below it when nothing is negative.
        if has_bar and yscale == "linear" and min(float(np.nanmin(c)) for c in lim_y) >= 0:
            ylim[0] = 0.0
        x0 = recipe[0]["x"]
        ys0 = recipe[0]["ys"]
        title = text_or(pn.get("title"), _plain(stem) if i == 0 else "")
        ylabel = text_or(pn.get("ylabel"), _plain(ys0[0]) if len(ys0) == 1 else "value")
        series = behind + front
        out.append({
            "id": pid,
            "title": {"text": title, "loc": "left", "size": 14, "color": "#000000"},
            "xlabel": {"text": text_or(pn.get("xlabel"), _plain(x0) if x0 else ""), "size": 13, "color": "#000000"},
            "ylabel": {"text": ylabel, "size": 13, "color": "#000000"},
            "xlim": xlim,
            "ylim": ylim,
            "xscale": xscale, "yscale": yscale, "aspect": "auto",
            "xtick_size": 11, "ytick_size": 11, "frame_lw": 0.8,
            "legend": {"loc": "best", "frameon": False, "size": 10, "xy": None}
                      if sum(1 for c in series if c.get("kind") not in GRID_KINDS + ("box",)) > 1 else None,
            "series": series,
            "arrows": [],
            "texts": [],
        })
        src = dict(recipe[0], title=pn.get("title"), xlabel=pn.get("xlabel"),
                   ylabel=pn.get("ylabel"), xscale=xscale, yscale=yscale)
        if len(recipe) > 1:
            src["more"] = recipe[1:]
        source.append(src)

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


def add_batch(spec, project, text, panel_id, batch):
    """More curves for one panel of an existing csv-panels figure, keeping
    every edit made so far. -> (new_spec, arrays, added ids). The batch is
    recorded in spec.source so Start over rebuilds it too; the whole recipe
    is rebuilt from `text` to prove it still holds together."""
    import copy
    src = copy.deepcopy(spec.get("source") or {})
    if src.get("kind") == "csv":
        # A New-from-CSV figure: the same recipe, in the multi-panel form.
        src = {"kind": "csv-panels", "file": src.get("file", "data/source.csv"),
               "filename": src.get("filename", ""), "figure_title": "",
               "panels": [{"x": src["x"], "ys": src["ys"], "kind": src.get("plot", "line")}]}
    if src.get("kind") != "csv-panels":
        raise CSVError("curves can only be added to figures made from a table")
    ids = [p["id"] for p in spec["panels"]]
    if panel_id not in ids:
        raise CSVError(f"no panel '{panel_id}'")
    i = ids.index(panel_id)
    src["panels"][i].setdefault("more", []).append(batch)
    full, arrays = build_panels(project, text, src["panels"], src.get("filename", ""),
                                src.get("figure_title", ""))
    new = copy.deepcopy(spec)
    have = {s["id"] for p in new["panels"] for s in p["series"]}
    added = [s for s in full["panels"][i]["series"] if s["id"] not in have]
    p = new["panels"][i]
    for s in added:
        if s.get("kind") in ("band", "bar"):
            p["series"].insert(0, s)      # behind the lines and points
        else:
            p["series"].append(s)
    if len(p["series"]) > 1 and not p.get("legend"):
        p["legend"] = {"loc": "best", "frameon": False, "size": 10, "xy": None}
    new["source"] = full["source"]
    return new, arrays, [s["id"] for s in added]


def merge_tables(base_text, extra_text):
    """One table with base's numeric columns plus extra's new ones, side by
    side (shorter columns padded with blanks). Names already in base keep
    base's data. -> (csv text, names taken from extra)."""
    import csv as _csv
    import io as _io
    bn, bc, _ = parse(base_text)
    en, ec, _ = parse(extra_text)
    names, cols = [], []
    for n, c in zip(bn, bc):
        if c is not None and n not in names:
            names.append(n); cols.append(c)
    added = []
    for n, c in zip(en, ec):
        if c is not None and n not in names:
            names.append(n); cols.append(c); added.append(n)
    rows = max(len(c) for c in cols)
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(names)
    for r in range(rows):
        w.writerow(["" if r >= len(c) or math.isnan(c[r]) else repr(float(c[r])) for c in cols])
    return buf.getvalue(), added
