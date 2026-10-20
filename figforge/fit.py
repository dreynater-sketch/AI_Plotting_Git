"""Fit a curve to a line's points: a straight line, a polynomial, an
exponential, a power law, a Gaussian or Lorentzian peak, or a formula the
user types. Only numpy (the server has no scipy): polynomials by linear
least squares, everything else by Levenberg-Marquardt.

A typed formula is parsed with ast and walked by hand -- never eval'd. It may
use x, numbers, + - * / **, pi, e, a few numpy functions, and single letters
(other than x and e) as the parameters to fit: "a*exp(-x/t) + c".

    fit(x, y, model, formula=None, sigma=None) -> {
        "params": [{"name", "value", "error"}], "r2", "text", "f": callable}
"""

import ast
import math

import numpy as np

MODELS = {
    "line": "y = a·x + b",
    "poly2": "y = a·x² + b·x + c",
    "poly3": "y = a·x³ + b·x² + c·x + d",
    "exp": "y = a·e^(b·x) + c",
    "power": "y = a·x^b",
    "gauss": "y = a·e^(−(x−m)²/(2s²)) + c",
    "lorentz": "y = a / (1 + ((x−m)/w)²) + c",
    "formula": "your formula",
}
MAX_POINTS = 200_000


class FitError(ValueError):
    pass


# ------------------------------------------------------------ typed formulas

_FUNCS = {"exp": np.exp, "log": np.log, "ln": np.log, "log10": np.log10, "sqrt": np.sqrt,
          "sin": np.sin, "cos": np.cos, "tan": np.tan, "tanh": np.tanh, "abs": np.abs,
          "arctan": np.arctan, "atan": np.arctan, "sinh": np.sinh, "cosh": np.cosh}
_CONSTS = {"pi": math.pi, "e": math.e}
_BIN = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide,
        ast.Pow: np.power}


def parse_formula(text):
    """-> (tree, parameter names in order of appearance). Raises FitError
    with a plain message for anything but arithmetic on x and parameters."""
    text = (text or "").strip().replace("^", "**")
    if text.lower().startswith("y") and "=" in text:
        text = text.split("=", 1)[1].strip()
    if not text or len(text) > 300:
        raise FitError("type a formula in x, like a*exp(-x/t) + c")
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError:
        raise FitError("that formula doesn't read right (check the brackets and * signs)") from None
    names = []

    def walk(n):
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            walk(n.left); walk(n.right)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            walk(n.operand)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS \
                and len(n.args) == 1 and not n.keywords:
            walk(n.args[0])
        elif isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            pass
        elif isinstance(n, ast.Name):
            if n.id in _FUNCS:
                raise FitError(f"{n.id} needs brackets, like {n.id}(x)")
            if n.id not in ("x",) + tuple(_CONSTS) and n.id not in names:
                if len(n.id) > 6:
                    raise FitError(f"'{n.id}' isn't something the formula knows (parameters are short "
                                   "names like a, b, t0)")
                names.append(n.id)
        else:
            raise FitError("only numbers, x, parameters, + − × ÷ ^ and exp/log/sqrt/sin/cos… work here")
    walk(tree)
    if not names:
        raise FitError("the formula needs at least one parameter to fit, like a or b")
    if len(names) > 8:
        raise FitError("at most 8 parameters")
    return tree, names


def _evaluate(tree, x, values):
    def ev(n):
        if isinstance(n, ast.BinOp):
            return _BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.Call):
            return _FUNCS[n.func.id](ev(n.args[0]))
        if isinstance(n, ast.Constant):
            return float(n.value)
        if n.id == "x":
            return x
        if n.id in values:
            return values[n.id]
        return _CONSTS[n.id]
    with np.errstate(all="ignore"):
        return np.broadcast_to(np.asarray(ev(tree), dtype=float), np.shape(x))


# ------------------------------------------------------------ built-in shapes

def _shape(model, x, y):
    """-> (names, f(x, p), first guess p0) for the non-polynomial shapes."""
    span = float(np.ptp(x)) or 1.0
    if model == "exp":
        c0 = float(np.min(y)) - 0.05 * (float(np.ptp(y)) or 1.0) if y[0] > y[-1] else 0.0
        pos = y - c0 > 0
        if pos.sum() >= 2:
            b, la = np.polyfit(x[pos], np.log(y[pos] - c0), 1)
        else:
            b, la = -1.0 / span, math.log(float(np.max(np.abs(y))) or 1.0)
        return ["a", "b", "c"], lambda x, p: p[0] * np.exp(p[1] * x) + p[2], [math.exp(la), b, c0]
    if model == "power":
        ok = (x > 0) & (y > 0)
        if ok.sum() < 2:
            raise FitError("a power law needs x and y above zero")
        b, la = np.polyfit(np.log(x[ok]), np.log(y[ok]), 1)
        return ["a", "b"], lambda x, p: p[0] * np.power(x, p[1]), [math.exp(la), b]
    if model in ("gauss", "lorentz"):
        c0 = float(np.median(np.concatenate([y[:max(1, len(y) // 10)], y[-max(1, len(y) // 10):]])))
        i = int(np.argmax(np.abs(y - c0)))
        a0, m0 = float(y[i] - c0), float(x[i])
        half = np.abs(y - c0) >= abs(a0) / 2
        width = float(np.ptp(x[half])) if half.sum() > 1 else span / 10
        if model == "gauss":
            return ["a", "m", "s", "c"], \
                lambda x, p: p[0] * np.exp(-(x - p[1]) ** 2 / (2 * p[2] ** 2)) + p[3], \
                [a0, m0, max(width / 2.355, span / 1000), c0]
        return ["a", "m", "w", "c"], lambda x, p: p[0] / (1 + ((x - p[1]) / p[2]) ** 2) + p[3], \
            [a0, m0, max(width / 2, span / 1000), c0]
    raise FitError(f"unknown shape '{model}'")


def _lm(f, x, y, p0, w, iters=200):
    """Levenberg-Marquardt on weighted residuals w*(y - f). -> (p, J at p)."""
    p = np.asarray(p0, dtype=float)
    lam = 1e-3

    def cost(q):
        r = w * (y - f(x, q))
        return float(np.dot(r, r)) if np.all(np.isfinite(r)) else math.inf

    def jac(q):
        J = np.empty((x.size, q.size))
        f0 = f(x, q)
        for k in range(q.size):
            h = 1e-7 * max(abs(q[k]), 1e-8)
            dq = q.copy(); dq[k] += h
            J[:, k] = w * (f(x, dq) - f0) / h
        return J

    c = cost(p)
    if not math.isfinite(c):
        raise FitError("the formula can't be worked out at the first guess (try other numbers or a "
                       "range of points where it's defined)")
    with np.errstate(all="ignore"):
        for _ in range(iters):
            J = jac(p)
            r = w * (y - f(x, p))
            A, g = J.T @ J, J.T @ r
            improved = False
            while lam < 1e12:
                try:
                    step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), g)
                except np.linalg.LinAlgError:
                    lam *= 10
                    continue
                c_new = cost(p + step)
                if c_new < c:
                    p, lam, improved = p + step, max(lam / 10, 1e-12), True
                    done = c - c_new <= 1e-12 * max(c, 1e-300)
                    c = c_new
                    break
                lam *= 10
            if not improved or done:
                break
    return p, jac(p)


def fit(x, y, model, formula=None, sigma=None):
    x, y = np.asarray(x, dtype=float).ravel(), np.asarray(y, dtype=float).ravel()
    if x.size != y.size:
        raise FitError("x and y have different lengths")
    ok = np.isfinite(x) & np.isfinite(y)
    w = np.ones_like(y)
    if sigma is not None:
        s = np.abs(np.asarray(sigma, dtype=float).ravel())
        if s.size == y.size:
            ok &= np.isfinite(s) & (s > 0)
            w = 1 / np.where(s > 0, s, 1)
    x, y, w = x[ok], y[ok], w[ok]
    if x.size > MAX_POINTS:
        raise FitError(f"that's over {MAX_POINTS:,} points; pick a smaller range")

    if model in ("line", "poly2", "poly3"):
        deg = {"line": 1, "poly2": 2, "poly3": 3}[model]
        names = list("abcd"[:deg + 1])
        if x.size <= deg:
            raise FitError(f"that needs at least {deg + 1} points")
        V = np.vander(x, deg + 1) * w[:, None]
        p, *_ = np.linalg.lstsq(V, y * w, rcond=None)
        J = V
        f = lambda x, p: np.polyval(p, x)
    else:
        if model == "formula":
            tree, names = parse_formula(formula)
            f = lambda x, p: _evaluate(tree, x, dict(zip(names, p)))
            p0 = np.ones(len(names))
        else:
            names, f, p0 = _shape(model, x, y)
        if x.size <= len(names):
            raise FitError(f"that needs more than {len(names)} points")
        p, J = _lm(f, x, y, p0, w)

    resid = y - f(x, p)
    ss = float(np.sum((w * resid) ** 2))
    dof = max(x.size - len(names), 1)
    try:
        cov = np.linalg.inv(J.T @ J) * (ss / dof)
        err = np.sqrt(np.abs(np.diag(cov)))
    except np.linalg.LinAlgError:
        err = np.full(len(names), np.nan)
    tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1 - float(np.sum(resid ** 2)) / tot if tot > 0 else 1.0
    if not np.all(np.isfinite(p)):
        raise FitError("the fit didn't settle -- try a different shape or range")
    params = [{"name": n, "value": float(v), "error": float(e) if np.isfinite(e) else None}
              for n, v, e in zip(names, p, err)]
    text = (formula.strip() if model == "formula" else MODELS[model])
    return {"params": params, "r2": r2, "text": text, "n": int(x.size),
            "f": lambda xx: f(np.asarray(xx, dtype=float), p)}


def nice(v, e=None):
    """2.31 ± 0.04 style: the value to the error's precision (2 significant
    figures of the error), or 4 significant figures without one."""
    if e is None or not math.isfinite(e) or e <= 0:
        return f"{v:.4g}"
    digits = max(0, 1 - int(math.floor(math.log10(e))))
    if abs(v) >= 1e5 or (abs(v) < 1e-3 and v != 0):
        exp = int(math.floor(math.log10(abs(v))))
        m, me = v / 10 ** exp, e / 10 ** exp
        d = max(0, 1 - int(math.floor(math.log10(me)))) if me > 0 else 2
        return f"({m:.{d}f} ± {me:.{d}f})×10^{exp}"
    return f"{v:.{digits}f} ± {e:.{digits}f}"
