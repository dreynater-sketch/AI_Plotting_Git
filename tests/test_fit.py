"""Fit a curve: every shape finds known numbers (with sensible ± errors), a
typed formula is read safely and never run, and in the editor a picked line
gets a fitted curve (dashed, in the legend), its numbers, a label with them,
a fit again replaces it, Undo takes it away, a bad formula says why, and the
figure's code reads back unchanged."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import json, math, os, sys, urllib.error, urllib.request
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
import numpy as np

from figforge import codegen, codesync, csvimport, fit as F
from figforge.project import Figure, LocalStore

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

rng = np.random.default_rng(0)
x = np.linspace(0.5, 10, 80)
noise = rng.normal(0, 0.03, x.size)
truth = {
    "line": (2.3 * x + 1, [2.3, 1]), "poly2": (0.5 * x ** 2 - x + 3, [0.5, -1, 3]),
    "exp": (5 * np.exp(-0.4 * x) + 1, [5, -0.4, 1]), "power": (3 * x ** 1.7, [3, 1.7]),
    "gauss": (4 * np.exp(-(x - 6) ** 2 / (2 * 0.8 ** 2)) + 0.5, [4, 6, 0.8, 0.5]),
    "lorentz": (4 / (1 + ((x - 6) / 0.7) ** 2) + 0.5, [4, 6, 0.7, 0.5]),
}
for model, (y, want) in truth.items():
    r = F.fit(x, y + noise, model)
    got = [p["value"] for p in r["params"]]
    close = all(abs(g - w) <= max(5 * p["error"], 0.02 * abs(w)) for g, w, p in zip(got, want, r["params"]))
    check(close and r["r2"] > 0.99, f"{model}: {[F.nice(p['value'], p['error']) for p in r['params']]}")
r = F.fit(x, 5 * np.exp(-x / 2.5) + 1 + noise, "formula", "y = a*exp(-x/t) + c")
check([p["name"] for p in r["params"]] == ["a", "t", "c"] and abs(r["params"][1]["value"] - 2.5) < 0.1,
      "a typed formula: its letters are the numbers worked out")
for bad in ('__import__("os").system("x")', "x.real*a", "open(a)", "a*x; b", "(lambda: a)()", "a" * 400):
    try:
        F.parse_formula(bad)
        check(False, f"refused: {bad[:30]}")
    except F.FitError:
        check(True, f"refused: {bad[:30]}")
check(F.nice(2.30271, 0.0018) == "2.3027 ± 0.0018" and F.nice(123456.0, 789.0).startswith("(1.2346 ± 0.0079)×10^5"),
      f"numbers shown to the error's precision ({F.nice(123456.0, 789.0)})")

# ------------------------------------------------------------- the editor
P = "zz_fit"
rmtree(f"figures/{P}")
TEXT = "time,signal\n" + "\n".join(f"{t:.3f},{2.5 * math.exp(-t / 3) + 0.4 + n:.5f}"
                                    for t, n in zip(np.linspace(0, 12, 60), rng.normal(0, 0.02, 60)))
spec, arrays = csvimport.build("zz_fit", TEXT, 0, [1], "scatter", "decay.csv")
fig = Figure(P, LocalStore()); fig.save_arrays(arrays); fig.save_source_csv(TEXT); fig.save_spec(spec)

B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765")
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))
from playwright.sync_api import sync_playwright
try:
    req = urllib.request.Request(f"{B}/api/fit/{P}", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"series_id": "a_s0", "model": "formula",
                                                  "formula": "__import__('os')"}).encode())
    try:
        urllib.request.urlopen(req); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check(code == 400, "the server refuses code in a formula")

    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        pg = br.new_page(viewport={"width": 1400, "height": 900}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{B}/?project={P}")
        pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
        settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
        ev = pg.evaluate

        def do_fit():
            pg.click("#btn-fit-curve")
            pg.wait_for_function("!fitting", timeout=30000)
            settle()

        pg.click("#menu-insert"); pg.click("#menu-add-fit")
        check(ev("selection[0]") == "a_s0" and pg.locator("#fit-fields").is_visible(),
              "Insert > Fit a curve picks the dots and shows the fit box")
        pg.select_option("#f-fit-model", "exp")
        check("e^" in pg.inner_text("#fit-shape"), "the shape's formula is shown")
        do_fit()
        s = next(c for c in disk()["panels"][0]["series"] if c["id"] == "a_s0_fit")
        b = next(p for p in s["fit"]["params"] if p["name"] == "b")["value"]
        check(abs(b + 1 / 3) < 0.02 and s["style"]["ls"] == "--" and len(s["x"]) == 200,
              f"Fit adds a dashed fitted curve (b = {b:.4f}, want −0.333)")
        check(disk()["panels"][0].get("legend") and s["label"].startswith("fit:"), "…named in the legend")
        check(pg.locator("#fit-result").is_visible() and "R² = 0.99" in pg.inner_text("#fit-numbers"),
              f"its numbers show ({pg.inner_text('#fit-numbers').splitlines()[-1]})")
        pg.click("#btn-fit-label"); settle()
        t = disk()["panels"][0]["texts"][-1]
        check(t["coords"] == "axes" and "b = " in t["text"] and "±" in t["text"], "the numbers go on the plot as a label")

        ev("setSelection(['a_s0'])")
        pg.select_option("#f-fit-model", "line")
        pg.fill("#f-fit-from", "6"); pg.fill("#f-fit-to", "12")
        do_fit()
        fits = [c for c in disk()["panels"][0]["series"] if c.get("fit")]
        check(len(fits) == 1 and fits[0]["fit"]["model"] == "line" and min(fits[0]["x"]) >= 6,
              "fitting again replaces the earlier fit, over just the chosen range")

        ev("setSelection(['a_s0'])")
        pg.select_option("#f-fit-model", "formula")
        pg.fill("#f-fit-formula", "a*exp(-x/t")
        do_fit()
        check("bracket" in pg.inner_text("#status"), f"a broken formula says why ({pg.inner_text('#status')})")
        pg.fill("#f-fit-formula", "a*exp(-x/t) + c"); pg.fill("#f-fit-from", ""); pg.fill("#f-fit-to", "")
        do_fit()
        f = next(c for c in disk()["panels"][0]["series"] if c.get("fit"))["fit"]
        tval = next(p for p in f["params"] if p["name"] == "t")["value"]
        check(f["model"] == "formula" and abs(tval - 3) < 0.15, f"my own formula fits (t = {tval:.3f}, want 3)")

        pg.click("#btn-undo"); settle()
        f = next(c for c in disk()["panels"][0]["series"] if c.get("fit"))["fit"]
        check(f["model"] == "line", "Undo brings back the fit before")

        sp = disk()
        new, rep = codesync.apply(codegen.generate(sp), sp, list(arrays))
        check(new == sp and not rep["skipped"], f"the figure's code reads back unchanged {rep}")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
