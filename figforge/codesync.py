"""figure.py -> SPEC: apply a hand-edited script back onto the figure.

The code editor saves the user's edited figure.py; this reads it and folds
every change it recognises back into the spec, so the draggable figure
follows the code. It is the inverse of codegen.py and only understands the
shapes codegen writes -- top-level statements with literal arguments:

    fig.suptitle(...), plt.subplots(..., figsize=...), plt.rcParams.update({...})
    ax = axes[0, i]  (or axes[i])         which panel the lines below belong to
    ax.text / ax.plot / ax.add_patch(FancyArrowPatch(...))
    ax.set_title / set_xlabel / set_ylabel / set_xlim / set_ylim / set_xscale ...
    ax.axhline / ax.axvline, ax.tick_params, ax.legend, the spines loop
    fig.tight_layout(rect=...), fig.savefig(..., dpi=...)

Texts, curves and arrows are matched to the spec by the gid= codegen puts
on each (older code without gids falls back to matching by order). A call
with no known gid becomes a new element; an element whose call is gone is
removed. Anything else -- loops, functions, variables, computed values -- is
left in the user's code, untouched, and reported line by line; an element
whose own line can't be applied keeps its current settings (it is never
treated as deleted).

The code is only ever parsed (ast) and its literals read (ast.literal_eval);
nothing in it is executed.
"""

import ast
import copy
import math
from figforge import layers

# Statements codegen emits that carry nothing for the spec.
_BOILERPLATE_CALLS = {"plt.show", "fig.patch.set_facecolor", "_t.get_bbox_patch.set_clip_on"}


class Skip(Exception):
    """This statement can't be applied; the reason is reported to the user."""


def _name(node):
    """Dotted name of a call target: ax.set_xlim, plt.rcParams.update, ..."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):  # _t.get_bbox_patch().set_clip_on
        return _name(node.func)
    return None


def _gid_of(node):
    """The gid= literal anywhere in a statement, if it has one."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.keyword) and sub.arg == "gid" and isinstance(sub.value, ast.Constant):
            return sub.value.value
    return None


def _lit(node, what="value"):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise Skip(f"{what} isn't a plain value") from None


def _kwargs(call):
    out = {}
    for kw in call.keywords:
        if kw.arg is None:
            raise Skip("**kwargs can't be applied")
        out[kw.arg] = kw.value
    return out


def _coord(node):
    """A series coordinate: D["key"] or a literal list of numbers."""
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id == "D"):
        key = _lit(node.slice, "data key")
        if isinstance(key, str):
            return key
    val = _lit(node, "curve data")
    if isinstance(val, (list, tuple)) and all(isinstance(v, (int, float)) for v in val):
        return [float(v) for v in val]
    raise Skip("curve data must be D[\"...\"] or a list of numbers")


def _put(item, field, value, default=None):
    """Set a field only if it actually differs from what's there (or from
    codegen's default when it's absent) -- so code that wasn't edited maps
    back to exactly the spec it came from, not to spelled-out defaults."""
    if item.get(field, default) != value:
        item[field] = value


def _same_numbers(parsed, old):
    """codegen writes inline coordinates rounded to 10 digits."""
    return (isinstance(old, list) and len(old) == len(parsed)
            and all(round(float(o), 10) == p for o, p in zip(old, parsed)))


def _num(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Skip(f"{what} must be a number")
    return v


class _Panel:
    def __init__(self, old):
        self.old = old
        # Lists and switches the code spells out in full start empty/off.
        self.p = {k: v for k, v in old.items()
                  if k not in ("texts", "series", "arrows", "hlines", "vlines", "spans",
                               "grid", "minor_ticks", "xfmt", "yfmt")}
        self.p["texts"], self.p["series"], self.p["arrows"] = [], [], []
        self.p["hlines"], self.p["vlines"], self.p["spans"] = [], [], []
        self.p["zero_lines"] = False
        self.p["legend"] = None
        self.saw = set()      # which one-per-panel settings the code spelled out
        self.all_ids = set()  # every element id in the figure, to keep new ids unique
        self.gidless = {"texts": 0, "series": 0, "arrows": 0}


def apply(code, spec, array_keys=()):
    """Return (new_spec, report). The spec passed in is not modified."""
    tree = ast.parse(code)
    old = copy.deepcopy(spec)
    new = copy.deepcopy(spec)
    panels = [_Panel(p) for p in old["panels"]]
    every_id = {i["id"] for p in old["panels"] for k in ("texts", "series", "arrows")
                for i in p.get(k, [])}
    for pnl in panels:
        pnl.all_ids = every_id
    lines = code.splitlines()
    report = {"skipped": [], "added": [], "removed": [], "changed": []}
    state = {"ax": None, "suptitle": False}
    new["data_edits"] = {}   # the code's D["k"][i] = v lines are the whole truth
    taken = set()

    def skip(node, reason):
        text = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
        report["skipped"].append({"line": node.lineno, "code": text[:120], "reason": reason})

    # Code from before gids existed is matched by order; once any gid is
    # present, a call without one is always a new element.
    legacy = not any(isinstance(n, ast.keyword) and n.arg == "gid" for n in ast.walk(tree))
    for pnl in panels:
        pnl.legacy = legacy
    kept = set()  # elements whose line couldn't be applied: left exactly as they were
    for node in _attach_box_gids(tree.body):
        try:
            _statement(node, new, panels, state, taken, array_keys)
        except Skip as e:
            skip(node, str(e))
            gid = _gid_of(node)
            if isinstance(gid, str) and gid in every_id:
                kept.add(gid)
                taken.add(gid)

    if not new.get("data_edits"):
        new.pop("data_edits", None)
    if not state["suptitle"] and new.get("suptitle"):
        new["suptitle"]["text"] = ""

    # Whatever the code didn't mention per panel falls back to "not shown".
    for pnl in panels:
        for key in ("title", "xlabel", "ylabel"):
            if key not in pnl.saw and pnl.p.get(key):
                pnl.p[key] = {**pnl.p[key], "text": ""}
        for key, default in (("xscale", "linear"), ("yscale", "linear"), ("aspect", "auto")):
            if key not in pnl.saw:
                pnl.p[key] = default
        for key in ("top_axis", "right_axis"):
            if key not in pnl.saw:
                pnl.p.pop(key, None)
        if "twin" not in pnl.saw:
            pnl.p.pop("twin", None)
            pnl.p.pop("ylabel2", None)
        elif "ylabel2" not in pnl.saw and pnl.p.get("ylabel2"):
            pnl.p["ylabel2"] = {**pnl.p["ylabel2"], "text": ""}

    for pnl in panels:
        for kind in ("texts", "series", "arrows"):
            placed = {i["id"] for q in panels for i in q.p[kind]}
            pnl.p[kind] += [copy.deepcopy(i) for i in pnl.old.get(kind, [])
                            if i["id"] in kept and i["id"] not in placed]
        for key in ("hlines", "vlines", "spans"):
            if key not in pnl.old and not pnl.p[key]:
                del pnl.p[key]
        if "zero_lines" not in pnl.old and not pnl.p["zero_lines"]:
            del pnl.p["zero_lines"]
    for pnl in panels:
        for item in pnl.p["series"]:
            item.pop("_old_colorbar", None)
    new["panels"] = [pnl.p for pnl in panels]
    _diff(old, new, report)
    return new, report


def _statement(node, new, panels, state, taken, array_keys):
    # Module docstring, imports, HERE = ..., D = np.load(...): boilerplate.
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        return
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        # D["key"][row] = value: a number changed by hand
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Subscript)                 and _name(target.value.value) == "D":
            key = _lit(target.value.slice, "data key")
            row = _lit(target.slice, "row")
            value = _lit(node.value, "value")
            if not isinstance(key, str) or (array_keys and key not in array_keys):
                raise Skip(f'there is no D["{key}"] in the data')
            if not isinstance(row, int) or isinstance(row, bool) or row < 0:
                raise Skip("the row must be a whole number, 0 or more")
            new.setdefault("data_edits", {}).setdefault(key, {})[str(row)] = float(_num(value, "value"))
            return
        tname = _name(target)
        if tname in ("HERE", "D"):
            return
        # fig, axes = plt.subplots(1, n, figsize=(w, h))
        if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Call) \
                and _name(node.value.func) == "plt.subplots":
            call = node.value
            args = [_lit(a, "subplot count") for a in call.args]
            if "squeeze" in _kwargs(call) and _lit(_kwargs(call)["squeeze"], "squeeze") is not False:
                raise Skip("squeeze=True would break `axes[0, i]`")
            rows, cols = (args + [1, 1])[:2] if len(args) >= 2 else (1, args[0] if args else len(panels))
            if not all(isinstance(v, int) and v >= 1 for v in (rows, cols)) or rows * cols < len(panels):
                raise Skip("changing the number of panels isn't supported")
            state["cols"] = cols
            if (rows, cols) == (1, len(panels)):
                new.pop("grid_shape", None)
            else:
                new["grid_shape"] = [rows, cols]
            kw = _kwargs(call)
            if "figsize" in kw:
                w, h = _lit(kw["figsize"], "figsize")
                new["size_in"] = [_num(w, "figure width"), _num(h, "figure height")]
            return
        # ax = axes[0, i]   (older code: ax = axes[i])
        if tname == "ax" and isinstance(node.value, ast.Subscript) \
                and _name(node.value.value) == "axes":
            i = _lit(node.value.slice, "panel index")
            if isinstance(i, tuple) and len(i) == 2:
                i = i[0] * state.get("cols", len(panels)) + i[1]
            if not isinstance(i, int) or not 0 <= i < len(panels):
                raise Skip("there's no panel with that index")
            state["ax"] = panels[i]
            state["ax2"] = None
            return
        # _t = ax.text(...), _m = ax.pcolormesh(...), _b = ax.boxplot(...), _cb = fig.colorbar(...)
        if tname in ("_t", "_m", "_b", "_cb") and isinstance(node.value, ast.Call):
            state["assigning_m"] = tname == "_m"
            if tname == "_m":
                state["last_mapped"] = None      # a skipped _m line mustn't leave an old one
            try:
                return _call(node.value, new, state, taken, array_keys)
            finally:
                state.pop("assigning_m", None)
        # ax2 = ax.twinx(): a second y-axis with its own data
        if tname == "ax2" and isinstance(node.value, ast.Call) and _name(node.value.func) == "ax.twinx":
            pnl = _panel(state)
            pnl.p["twin"] = {k: v for k, v in (pnl.old.get("twin") or {}).items() if k not in ("ylim", "yscale")}
            pnl.saw.add("twin")
            state["ax2"] = pnl
            return
        # _top = ax.secondary_xaxis('top', functions=(lambda v: a * v + b, ...))
        if tname in _SECOND_VARS and isinstance(node.value, ast.Call):
            return _second_axis(tname, node.value, state)
        raise Skip("variables aren't reflected in the figure")
    # for _ax in axes.flat[n:]: _ax.remove()   (empty cells of a panel grid)
    if isinstance(node, ast.For) and _name(node.target) == "_ax" and len(node.body) == 1 \
            and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Call) \
            and (_name(node.body[0].value.func) or "").endswith(".remove"):
        return
    # for _sp in ax.spines.values(): _sp.set_linewidth(v)
    if isinstance(node, ast.For) and _name(node.iter.func if isinstance(node.iter, ast.Call) else node.iter) == "ax.spines.values" \
            and len(node.body) == 1 and isinstance(node.body[0], ast.Expr) \
            and isinstance(node.body[0].value, ast.Call) \
            and (_name(node.body[0].value.func) or "").endswith("set_linewidth"):
        pnl = _panel(state)
        _put(pnl.p, "frame_lw", _num(_lit(node.body[0].value.args[0], "frame width"), "frame width"), 0.8)
        return
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        return _call(node.value, new, state, taken, array_keys)
    raise Skip({ast.For: "loops", ast.While: "loops", ast.If: "if-blocks",
                ast.FunctionDef: "functions", ast.With: "with-blocks"}
               .get(type(node), "this kind of statement") + " aren't reflected in the figure")


# variable -> (spec key, the axis method that makes it, its side, x or y)
_SECOND_VARS = {"_top": ("top_axis", "ax.secondary_xaxis", "top", "x"),
                "_right": ("right_axis", "ax.secondary_yaxis", "right", "y")}


def _linear(lam, what):
    """(scale, offset) of `lambda v: <linear expression in v>`, read from the
    syntax tree -- only numbers, v, + - * / and unary minus; never executed."""
    if not isinstance(lam, ast.Lambda) or len(lam.args.args) != 1:
        raise Skip(f"{what}: expected `lambda v: a * v + b`")
    var = lam.args.args[0].arg
    ops = {ast.Add: lambda x, y: x + y, ast.Sub: lambda x, y: x - y,
           ast.Mult: lambda x, y: x * y, ast.Div: lambda x, y: x / y}

    def ev(n, v):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name) and n.id == var:
            return v
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            x = ev(n.operand, v)
            return -x if isinstance(n.op, ast.USub) else x
        if isinstance(n, ast.BinOp) and type(n.op) in ops:
            return ops[type(n.op)](ev(n.left, v), ev(n.right, v))
        raise Skip(f"{what}: only `a * v + b` style formulas are reflected in the figure")

    try:
        f0, f1, f2 = (ev(lam.body, v) for v in (0.0, 1.0, 2.0))
    except ZeroDivisionError:
        raise Skip(f"{what}: division by zero")
    # 15 significant digits: drops float noise like 0.0020000000000000018.
    scale, offset = float(f"{f1 - f0:.15g}"), float(f"{f0:.15g}")
    if scale == 0 or abs(f2 - (2 * scale + offset)) > 1e-9 * max(1.0, abs(f2)):
        raise Skip(f"{what}: the formula must be a straight line (a * v + b, a not 0)")
    return scale, offset


def _second_axis(var, call, state):
    key, method, side, _ = _SECOND_VARS[var]
    if _name(call.func) != method:
        raise Skip(f"`{var} = ...` must be `{method}('{side}', functions=(...))`")
    pnl = _panel(state)
    if not call.args or _lit(call.args[0], "side") != side:
        raise Skip(f"`{var}` goes on the {side}")
    funcs = _kwargs(call).get("functions")
    if not isinstance(funcs, ast.Tuple) or len(funcs.elts) != 2:
        raise Skip("a second axis needs functions=(forward, inverse)")
    scale, offset = _linear(funcs.elts[0], f"{side} axis")
    old = pnl.old.get(key) or {}
    sa = dict(old or {"text": "", "size": 13, "color": "#000000"})
    sa.update(scale=scale, offset=offset, text="")
    # Round-trip exactly: codegen wrote the stored floats; a*1 + b - b can
    # differ from a in the last bit, so keep the stored value when it matches.
    for k, v in (("scale", scale), ("offset", offset)):
        if isinstance(old.get(k), (int, float)) and math.isclose(old[k], v, rel_tol=1e-12, abs_tol=1e-15):
            sa[k] = old[k]
    pnl.p[key] = sa
    pnl.saw.add(key)
    state.setdefault("second", {})[var] = (pnl, key)


def _second_axis_call(var, call, state):
    pnl, key = state.get("second", {}).get(var, (None, None))
    if pnl is None:
        raise Skip(f"`{var}` is used before `{var} = ax.secondary_...axis(...)`")
    xy = _SECOND_VARS[var][3]
    method = _name(call.func)[len(var) + 1:]
    kw = _kwargs(call)
    if method == "tick_params":
        return   # follows the panel's own tick size
    if method == f"set_{xy}label":
        sa = pnl.p[key]
        sa["text"] = _lit(call.args[0], "label text") if call.args else ""
        for k, field, default in (("fontsize", "size", 13), ("color", "color", "black")):
            if k in kw:
                _put(sa, field, _lit(kw[k], k), default)
        return
    raise Skip(f"`{var}.{method}` isn't reflected in the figure")


def _twin_call(method, call, args, kw, new, state, taken, array_keys):
    """ax2.<method>(...): curves on the right axis and its range/label."""
    pnl = state.get("ax2")
    if pnl is None or pnl is not state.get("ax"):
        raise Skip("`ax2` is used before `ax2 = ax.twinx()` for this panel")
    p = pnl.p
    tw = p["twin"]
    if method in layers.BY_METHOD:
        item = _series(method, args, kw, pnl, taken, array_keys)
        item["axis"] = "right"
        mapped = state.pop("assigning_m", False) and layers.KINDS[item.get("kind", "line")].get("colorbar")
        state["last_mapped"] = item if mapped else None
        state["colorbar"] = None
        return
    if method == "set_ylim":
        lo, hi = (_lit(a, "limit") for a in args[:2])
        tw["ylim"] = [None if v is None else _num(v, "limit") for v in (lo, hi)]
        return
    if method == "set_yscale":
        val = _lit(args[0], "scale")
        if val not in ("linear", "log"):
            raise Skip("only linear and log scales are supported")
        tw["yscale"] = val
        return
    if method == "tick_params":
        if "labelsize" in kw:
            _put(tw, "tick_size", _num(_lit(kw["labelsize"], "labelsize"), "labelsize"), p.get("ytick_size", 11))
        return
    if method == "set_ylabel":
        item = dict(pnl.old.get("ylabel2") or {})
        item["text"] = _lit(args[0], "label text") if args else ""
        for k, field, default in (("fontsize", "size", 13), ("color", "color", "black")):
            if k in kw:
                _put(item, field, _lit(kw[k], k), default)
        p["ylabel2"] = item
        pnl.saw.add("ylabel2")
        return
    raise Skip(f"`ax2.{method}` isn't reflected in the figure")


def _panel(state):
    if state["ax"] is None:
        raise Skip("this comes before any `ax = axes[i]` line")
    return state["ax"]


def _call(call, new, state, taken, array_keys):
    name = _name(call.func)
    kw = _kwargs(call)
    args = call.args

    if name in _BOILERPLATE_CALLS or name == "plt.show":
        return
    if name == "plt.rcParams.update":
        rc = _lit(args[0], "rcParams")
        if not isinstance(rc, dict):
            raise Skip("rcParams must be a dict")
        new["rcparams"] = rc
        return
    if name == "fig.suptitle":
        sup = dict(new.get("suptitle") or {})
        sup["text"] = _lit(args[0], "title text") if args else ""
        for k, key, default in (("fontsize", "size", 16), ("y", "y", 0.98), ("color", "color", "black")):
            if k in kw:
                _put(sup, key, _lit(kw[k], k), default)
        new["suptitle"] = sup
        state["suptitle"] = True
        return
    if name == "fig.tight_layout":
        layout = dict(new.get("layout") or {})
        _put(layout, "tight", True, True)
        if "rect" in kw:
            _put(layout, "rect", list(_lit(kw["rect"], "rect")), [0, 0, 1, 1])
        new["layout"] = layout
        return
    if name == "fig.savefig":
        if "dpi" in kw:
            _put(new, "dpi", _num(_lit(kw["dpi"], "dpi"), "dpi"), 200)
        return
    if name and name.startswith("ax2."):
        return _twin_call(name[4:], call, args, kw, new, state, taken, array_keys)
    if name in ("fig.colorbar", "_cb.set_label", "_cb.ax.tick_params"):
        return _colorbar_call(name, call, state)
    if name and name.split(".")[0] in _SECOND_VARS:
        return _second_axis_call(name.split(".")[0], call, state)
    if name == "ax.add_patch":
        if len(args) != 1 or not isinstance(args[0], ast.Call) \
                or _name(args[0].func) != "FancyArrowPatch":
            raise Skip("only FancyArrowPatch arrows are reflected in the figure")
        return _arrow(args[0], state, taken)
    if not name or not name.startswith("ax."):
        raise Skip(f"`{name or 'this call'}` isn't reflected in the figure")

    pnl = _panel(state)
    p = pnl.p
    method = name[3:]

    if method == "text":
        return _text(call, args, kw, pnl, taken)
    if method in layers.BY_METHOD:      # plot / errorbar / bar / pcolormesh / boxplot ...
        item = _series(method, args, kw, pnl, taken, array_keys)
        item.pop("axis", None)
        # Only the curve on an `_m = ...` line can get the next fig.colorbar(_m).
        mapped = state.pop("assigning_m", False) and layers.KINDS[item.get("kind", "line")].get("colorbar")
        state["last_mapped"] = item if mapped else None
        state["colorbar"] = None
        return
    if method in ("set_title", "set_xlabel", "set_ylabel"):
        key = method[4:]
        item = dict(pnl.old.get(key) or {})
        item["text"] = _lit(args[0], "label text") if args else ""
        for k, field, default in (("fontsize", "size", 14 if key == "title" else 13),
                                  ("color", "color", "black"), ("loc", "loc", "center")):
            if k in kw:
                _put(item, field, _lit(kw[k], k), default)
        p[key] = item
        pnl.saw.add(key)
        return
    if method in ("set_xlim", "set_ylim"):
        lo, hi = (_lit(a, "limit") for a in args[:2])
        p[method[4:]] = [_num(lo, "limit"), _num(hi, "limit")]
        return
    if method in ("set_xscale", "set_yscale"):
        val = _lit(args[0], "scale")
        if val not in ("linear", "log"):
            raise Skip("only linear and log scales are supported")
        p[method[4:]] = val
        pnl.saw.add(method[4:])
        return
    if method == "set_aspect":
        p["aspect"] = "equal" if args and _lit(args[0], "aspect") == "equal" else "auto"
        pnl.saw.add("aspect")
        return
    if method == "tick_params":
        axis = _lit(kw["axis"], "axis") if "axis" in kw else "both"
        if "labelsize" in kw:
            size = _num(_lit(kw["labelsize"], "labelsize"), "labelsize")
            for a in ("x", "y"):
                if axis in (a, "both"):
                    _put(p, f"{a}tick_size", size, 11)
        return
    if method in ("axvspan", "axhspan"):
        if len(args) < 2:
            raise Skip(f"ax.{method} needs where it starts and ends")
        olds = pnl.old.get("spans") or []
        n = len(p["spans"])
        sp = copy.deepcopy(olds[n]) if n < len(olds) else {}
        sp["axis"] = "x" if method == "axvspan" else "y"
        sp["lo"], sp["hi"] = (_num(_lit(a, "span edge"), "span edge") for a in args[:2])
        for k, default in (("color", "#cccccc"), ("alpha", 0.25)):
            if k in kw:
                _put(sp, k, _lit(kw[k], k), default)
        if "label" in kw:
            _put(sp, "label", _lit(kw["label"], "label"), "")
        else:
            sp.pop("label", None)
        p["spans"].append(sp)
        return
    if method == "minorticks_on":
        p["minor_ticks"] = True
        return
    if method == "set_axisbelow":
        return
    if method == "grid":
        on = _lit(args[0], "grid") if args else True
        which = _lit(kw["which"], "which") if "which" in kw else "major"
        if on:
            if which in ("minor", "both"):
                p["grid"] = "both"
            elif p.get("grid") != "both":
                p["grid"] = "major"
        return
    if method == "ticklabel_format":
        axis = _lit(kw["axis"], "axis") if "axis" in kw else "both"
        if "style" in kw and _lit(kw["style"], "style") in ("sci", "scientific"):
            for xy in ("x", "y"):
                if axis in (xy, "both"):
                    p.setdefault(f"{xy}fmt", {})["sci"] = True
        return
    if method in ("xaxis.set_major_locator", "yaxis.set_major_locator"):
        loc = args[0] if args else None
        if not (isinstance(loc, ast.Call) and _name(loc.func) == "MultipleLocator" and loc.args):
            raise Skip("only MultipleLocator(step) tick spacing is reflected in the figure")
        p.setdefault(f"{method[0]}fmt", {})["step"] = _num(_lit(loc.args[0], "tick step"), "tick step")
        return
    if method in ("axhline", "axvline"):
        pos = _num(_lit(args[0], "line position"), "line position") if args else 0
        style = {k: _lit(v, k) for k, v in kw.items()}
        if pos == 0 and style.get("color") == "0.88" and style.get("lw") == 0.7:
            p["zero_lines"] = True
            return
        key = "hlines" if method == "axhline" else "vlines"
        olds = pnl.old.get(key) or []
        n = len(p[key])
        line = copy.deepcopy(olds[n]) if n < len(olds) else {}
        line["y" if key == "hlines" else "x"] = pos
        for k, default in (("color", "0.8"), ("ls", "-"), ("lw", 1.0)):
            if k in style:
                _put(line, k, style[k], default)
        p[key].append(line)
        return
    if method == "legend":
        lg = dict(pnl.old.get("legend") or {})
        if "bbox_to_anchor" in kw:
            _put(lg, "xy", list(_lit(kw["bbox_to_anchor"], "legend position"))[:2])
        else:
            _put(lg, "xy", None)
            if "loc" in kw:
                _put(lg, "loc", _lit(kw["loc"], "loc"), "best")
        if "frameon" in kw:
            _put(lg, "frameon", bool(_lit(kw["frameon"], "frameon")), False)
        if "fontsize" in kw:
            _put(lg, "size", _num(_lit(kw["fontsize"], "fontsize"), "fontsize"), 10)
        p["legend"] = lg
        return
    raise Skip(f"`ax.{method}` isn't reflected in the figure")


def _claim(pnl, kind, kw, taken):
    """The existing element this call edits (by gid, else by order for
    old gid-less code), or None for a brand-new element."""
    olds = pnl.old.get(kind, [])
    if "gid" in kw:
        gid = _lit(kw["gid"], "gid")
        for item in olds:
            if item.get("id") == gid and gid not in taken:
                taken.add(gid)
                return copy.deepcopy(item)
        return {"id": gid} if isinstance(gid, str) and gid not in taken else None
    if not pnl.legacy:
        return None
    i = pnl.gidless[kind]
    pnl.gidless[kind] += 1
    if i < len(olds) and olds[i].get("id") not in taken:
        taken.add(olds[i]["id"])
        return copy.deepcopy(olds[i])
    return None


def _new_id(pnl, kind, taken):
    pid = pnl.p["id"]
    n = 1
    while f"{pid}_{kind}{n}" in taken or f"{pid}_{kind}{n}" in pnl.all_ids:
        n += 1
    taken.add(f"{pid}_{kind}{n}")
    return f"{pid}_{kind}{n}"


def _text(call, args, kw, pnl, taken):
    if len(args) < 3:
        raise Skip("ax.text needs x, y and the text")
    x, y = (_num(_lit(a, "position"), "position") for a in args[:2])
    item = _claim(pnl, "texts", kw, taken) or {"id": _new_id(pnl, "text", taken)}
    _put(item, "xy", [x, y])
    item["text"] = str(_lit(args[2], "text"))
    for k, field, default in (("fontsize", "size", 12), ("color", "color", "black"),
                              ("ha", "ha", "left"), ("va", "va", "baseline"),
                              ("zorder", "zorder", 10)):
        if k in kw:
            _put(item, field, _lit(kw[k], k), default)
    t = kw.get("transform")
    _put(item, "coords", "axes" if t is not None and _name(t) == "ax.transAxes" else "data", "data")
    if "bbox" in kw:
        box = _lit(kw["bbox"], "bbox")
        if not isinstance(box, dict):
            raise Skip("bbox must be a dict")
        item["bbox"] = box
    else:
        item.pop("bbox", None)
    pnl.p["texts"].append(item)


def _keys(node):
    """[D["a"], D["b"]] -> ["a", "b"] (a box plot's groups)."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise Skip("box plot groups must be a list like [D[\"a\"], D[\"b\"]]")
    out = []
    for el in node.elts:
        ref = _coord(el)
        if not isinstance(ref, str):
            raise Skip("box plot groups must be D[\"...\"] arrays")
        out.append(ref)
    return out


def _is_data(node):
    """D["key"] or a list of numbers -- data, not a style value."""
    return ((isinstance(node, ast.Subscript) and _name(node.value) == "D")
            or isinstance(node, (ast.List, ast.Tuple)))


def _series(method, args, kw, pnl, taken, array_keys):
    kind = layers.BY_METHOD[method]
    k = layers.KINDS[kind]
    lists = k.get("lists", ())
    want = len(k["coords"]) + len(lists)
    if len(args) != want:
        names = list(k["coords"]) + list(lists)
        raise Skip(f"ax.{method} takes {', '.join(names)} here ({want} data argument"
                   f"{'' if want == 1 else 's'})")
    refs = {f: _coord(a) for f, a in zip(k["coords"], args)}
    as_data = set()
    for kwname, field in k["data_kw"].items():
        # s=D["sizes"] is data; s=20 is a style value (both are matplotlib's s=).
        if kwname in kw and _is_data(kw[kwname]):
            refs[field] = _coord(kw[kwname])
            as_data.add(kwname)
    lrefs = {f: _keys(a) for f, a in zip(lists, args[len(k["coords"]):])}
    for ref in list(refs.values()) + [r for v in lrefs.values() for r in v]:
        if isinstance(ref, str) and array_keys and ref not in array_keys:
            raise Skip(f'there is no D["{ref}"] in the data')
    item = _claim(pnl, "series", kw, taken) or {"id": _new_id(pnl, "curve", taken)}
    if kind == "line":
        item.pop("kind", None)
    else:
        item["kind"] = kind
    # Data fields another kind used (the code changed plot -> bar, say) go.
    for field in _ALL_DATA_FIELDS - set(refs) - set(lrefs):
        item.pop(field, None)
    for field, val in refs.items():
        if not (isinstance(val, list) and _same_numbers(val, item.get(field))):
            item[field] = val
    item.update(lrefs)
    # Style keys codegen doesn't write (not matplotlib kwargs) survive.
    style = {key: v for key, v in (item.get("style") or {}).items() if key not in layers.ALL_STYLE}
    style.update(layers.style_from_kwargs(kind, {key: _lit(v, key) for key, v in kw.items()
                                                 if key in k["style"] and key not in as_data}))
    if style or "style" in item:
        item["style"] = style
    if "label" in kw:
        _put(item, "label", _lit(kw["label"], "label"), "")
    elif item.get("label") and kind in layers.LEGEND_KINDS:
        item.pop("label")      # (a heatmap's name isn't in the code: it stays)
    # The colour bar comes back only if the code still has its lines.
    item["_old_colorbar"] = item.pop("colorbar", None)
    unknown = set(kw) - k["style"] - set(k["data_kw"]) - set(k.get("fixed", {})) - {"label", "gid"}
    if unknown:
        raise Skip(f"{method} options {', '.join(sorted(unknown))} aren't supported")
    pnl.p["series"].append(item)
    return item


def _colorbar_call(name, call, state):
    """_cb = fig.colorbar(_m, ax=ax) / _cb.set_label(...) / _cb.ax.tick_params(...)."""
    item = state.get("last_mapped")
    kw = _kwargs(call)
    if name == "fig.colorbar":
        if item is None or not call.args or _name(call.args[0]) != "_m":
            raise Skip("fig.colorbar(_m, ax=ax) needs `_m = ax.pcolormesh(...)` (or contour, "
                       "contourf, scatter) just before it")
        cb = dict(item.get("_old_colorbar") or {})
        cb["label"] = ""
        cb.pop("tick_size", None)
        item["colorbar"] = cb
        state["colorbar"] = cb
        return
    cb = state.get("colorbar")
    if cb is None:
        raise Skip("this comes before `_cb = fig.colorbar(...)`")
    if name == "_cb.set_label":
        cb["label"] = _lit(call.args[0], "colour bar label") if call.args else ""
        for k, field, default in (("fontsize", "size", 12), ("color", "color", "black")):
            if k in kw:
                _put(cb, field, _lit(kw[k], k), default)
        return
    if name == "_cb.ax.tick_params":
        if "labelsize" in kw:
            _put(cb, "tick_size", _num(_lit(kw["labelsize"], "labelsize"), "labelsize"), 10)
        return
    raise Skip(f"`{name}` isn't reflected in the figure")


_ALL_DATA_FIELDS = {f for k in layers.KINDS.values()
                    for f in list(k["coords"]) + list(k.get("lists", ())) + list(k["data_kw"].values())}


def _attach_box_gids(body):
    """codegen writes `_b = ax.boxplot(...)` then `_b["boxes"][0].set_gid('id')`
    (boxplot takes no gid=). Fold the id into the boxplot call so it's
    claimed like every other curve, and drop the set_gid line."""
    out = []
    for node in body:
        prev = out[-1] if out else None
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "set_gid"
                and isinstance(prev, ast.Assign) and _name(prev.targets[0]) == "_b"
                and isinstance(prev.value, ast.Call) and _name(prev.value.func) == "ax.boxplot"
                and node.value.args):
            prev.value.keywords.append(ast.keyword(arg="gid", value=node.value.args[0]))
            continue
        out.append(node)
    return out


def _arrow(call, state, taken):
    pnl = _panel(state)
    kw = _kwargs(call)
    if len(call.args) < 2:
        raise Skip("FancyArrowPatch needs start and end points")
    p0, p1 = (_lit(a, "arrow point") for a in call.args[:2])
    item = _claim(pnl, "arrows", kw, taken) or {"id": _new_id(pnl, "arrow", taken) + "_arrow"}
    _put(item, "p0", [_num(p0[0], "arrow point"), _num(p0[1], "arrow point")])
    _put(item, "p1", [_num(p1[0], "arrow point"), _num(p1[1], "arrow point")])
    for k, default in (("arrowstyle", "->"), ("mutation_scale", 12), ("lw", 1.8),
                       ("color", "black"), ("zorder", 6)):
        if k in kw:
            _put(item, k, _lit(kw[k], k), default)
    pnl.p["arrows"].append(item)


# -------------------------------------------------------------- reporting

_FIELD_WORDS = {"xy": "position", "p0": "start", "p1": "end", "size": "size",
                "text": "text", "color": "colour", "bbox": "box", "style": "style",
                "label": "legend label", "x": "data", "y": "data"}


def _describe(item):
    return item.get("label") or item.get("text") or item.get("id")


def _diff(old, new, report):
    for po, pn in zip(old["panels"], new["panels"]):
        for kind in ("texts", "series", "arrows"):
            before = {i["id"]: i for i in po.get(kind, [])}
            after = {i["id"]: i for i in pn.get(kind, [])}
            for i in after:
                if i not in before:
                    report["added"].append(f"({po['id']}) {kind[:-1]} “{_describe(after[i])}”")
                elif after[i] != before[i]:
                    fields = sorted({_FIELD_WORDS.get(k, k) for k in set(before[i]) | set(after[i])
                                     if before[i].get(k) != after[i].get(k)})
                    report["changed"].append(f"({po['id']}) “{_describe(after[i])}”: {', '.join(fields)}")
            for i in before:
                if i not in after:
                    report["removed"].append(f"({po['id']}) {kind[:-1]} “{_describe(before[i])}”")
        for key in ("title", "xlabel", "ylabel", "xlim", "ylim", "xscale", "yscale",
                    "aspect", "xtick_size", "ytick_size", "frame_lw", "legend",
                    "hlines", "vlines", "zero_lines", "top_axis", "right_axis",
                    "spans", "grid", "minor_ticks", "xfmt", "yfmt", "twin", "ylabel2"):
            if po.get(key) != pn.get(key):
                report["changed"].append(f"({po['id']}) {key.replace('_', ' ')}")
    if (old.get("data_edits") or {}) != (new.get("data_edits") or {}):
        report["changed"].append("numbers changed by hand")
    for key in ("suptitle", "size_in", "rcparams", "layout", "dpi", "grid_shape"):
        if old.get(key) != new.get(key):
            report["changed"].append({"suptitle": "figure title", "size_in": "figure size"}
                                     .get(key, key))
