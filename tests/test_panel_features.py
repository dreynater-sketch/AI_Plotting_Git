"""Panel features: grid lines, small ticks, number format, shaded ranges, a
grid of plots and a second y-axis with its own data -- each drawn, exported
to figure.py and read back unchanged, reachable as AI tools, and set from
the editor (with Ctrl+Z)."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import copy, json, os, sys, urllib.request
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
from figforge import codegen, codesync, csvimport, ops, render
from playwright.sync_api import sync_playwright

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

# --- backend: every feature through the AI tools, then figure.py and back
rows = ["T,Q,Rs"] + [f"{t},{2e6 / (1 + 0.01 * t * t):.1f},{0.5 + 0.002 * t * t:.4f}" for t in range(2, 30)]
txt = "\n".join(rows)
spec, arr = csvimport.build_panels("t", txt, [{"x": "T", "ys": ["Q", "Rs"], "kind": "line+markers"},
                                              {"x": "T", "ys": ["Q"]}, {"x": "T", "ys": ["Rs"]}])
new, res, _ = ops.apply(spec, [
    {"name": "set_grid", "input": {"panel_id": "a", "grid": "both", "minor_ticks": True}},
    {"name": "set_tick_format", "input": {"panel_id": "b", "axis": "y", "step": 5e5, "scientific": True}},
    {"name": "add_span", "input": {"panel_id": "a", "axis": "x", "from": 10, "to": 15, "color": "#ffcc00",
                                   "opacity": 0.3, "label": "transition"}},
    {"name": "set_layout", "input": {"rows": 2, "columns": 2}},
    {"name": "set_curve_axis", "input": {"series_id": "a_s1", "axis": "right"}},
    {"name": "set_text", "input": {"element_id": "a__ylabel2", "text": "$R_s$ (n$\\Omega$)"}},
    {"name": "set_axis", "input": {"panel_id": "a", "axis": "right", "min": None, "max": 3, "scale": "keep",
                                   "tick_size": None}},
], arr)
check(not any(r["is_error"] for r in res), f"all AI tools succeed {[r['content'] for r in res if r['is_error']]}")
p = new["panels"][0]
check(p["grid"] == "both" and p["minor_ticks"] and p["spans"][0]["label"] == "transition", "grid, small ticks, span set")
check(new["grid_shape"] == [2, 2] and p["twin"]["ylim"] == [None, 3], "2 x 2 grid; right axis top only")
svg, _ = render.render(new, arr, preview=True)
check(all(g in svg for g in ("t_a__ylabel2", "t_a__span_0")), "right label and span are clickable")
code = codegen.generate(new)
check("ax2 = ax.twinx()" in code and "ax2.set_ylim(None, 3)" in code and "axes[1, 0]" in code,
      "figure.py uses plain matplotlib (twinx, subplots grid)")
back, rep = codesync.apply(code, new, list(arr))
check(back == new and not rep["skipped"], f"figure.py reads back unchanged {rep['skipped']}")
import numpy as np, matplotlib; matplotlib.use("Agg")
run_dir = _os.path.join(OUT_DIR, "panelfeat_run"); _os.makedirs(_os.path.join(run_dir, "data"), exist_ok=True)
np.savez(_os.path.join(run_dir, "data", "curves.npz"), **arr)
ns = {"__file__": _os.path.join(run_dir, "figure.py"), "__name__": "figure"}
exec(compile(code.replace("plt.show()", ""), "figure.py", "exec"), ns)   # our own generated code
check(len(ns["fig"].axes) == 4, "figure.py runs: 3 plots + 1 right axis")
_, res, _ = ops.apply(new, [{"name": "set_second_axis", "input": {"panel_id": "a", "side": "right", "show": True,
                                                                  "label": "x", "scale": 2, "offset": 0}}], arr)
check(res[0]["is_error"], "a calibrated right scale is refused while the right axis holds data")
back2, _, _ = ops.apply(new, [{"name": "set_curve_axis", "input": {"series_id": "a_s1", "axis": "left"}}], arr)
check("twin" not in back2["panels"][0] and "ylabel2" not in back2["panels"][0], "moving it back removes the right axis")
s2, a2 = csvimport.build_panels("t", txt, [{"x": "T", "ys": ["Q"], "right_ys": ["Rs"], "right_label": "R"}])
s3, _, _ = csvimport.add_batch(s2, "t", txt, "a", {"x": "T", "ys": ["Q"], "kind": "scatter"})
check(s2["panels"][0]["ylabel2"]["text"] == "R" and [c.get("axis") for c in s3["panels"][0]["series"]][:2] == [None, "right"],
      "builder: right_ys make a right axis that survives adding curves")

# --- legend columns + heading; bars clickable all over
lg_spec, _, _ = ops.apply(spec, [{"name": "set_legend", "input": {"panel_id": "a", "visible": True, "location": "keep",
                                  "font_size": None, "frame": None, "columns": 2, "title": "Data"}}], arr)
code = codegen.generate(lg_spec)
check("ncols=2, title='Data'" in code and codesync.apply(code, lg_spec, list(arr))[0] == lg_spec,
      "legend columns and heading go to figure.py and read back")
bar_spec, bar_arr = csvimport.build_panels("t", txt, [{"x": "T", "ys": [], "layers": [{"kind": "bar", "y": "Rs"}]}])
svg, _ = render.render(bar_spec, bar_arr, preview=True)
i = svg.find('id="t_a_l0"')
check(svg[i:svg.find("</g>", i)].count("<path") == 28, "every bar is its own click shape")

# --- editor
B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P = "zz_panelfeat"
rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B + "/api/project/duplicate", method="POST",
                       data=json.dumps({"from": "qcircle", "name": P}).encode(),
                       headers={"Content-Type": "application/json"}))
if os.path.exists(f"figures/{P}/history.json"):
    os.remove(f"figures/{P}/history.json")
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge"); pg = br.new_page(viewport={"width": 1400, "height": 850})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project={P}")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
    svg = lambda: pg.inner_html("#stage")

    pg.evaluate("setSelection(['panel:a'])")
    pg.select_option("#f-grid", "major"); settle()
    pg.check("#f-minor"); settle()
    pg.fill("#f-ystep", "0.5"); pg.press("#f-ystep", "Enter"); settle()
    pg.check("#f-xsci"); settle()
    a = disk()["panels"][0]
    check(a.get("grid") == "major" and a.get("minor_ticks") and a.get("yfmt") == {"step": 0.5}
          and a.get("xfmt") == {"sci": True}, "plot box: grid lines, small ticks, number every 0.5, ×10ⁿ")
    check(pg.is_hidden("#twin-fields"), "no right-axis fields without a right axis")

    pg.click("#btn-add-span"); settle()
    sid = pg.evaluate("selection[0]")
    pg.fill("#f-span-label", "edge"); settle()
    check(sid == "a__span_0" and disk()["panels"][0]["spans"][0]["label"] == "edge" and "t_a__span_0" in svg(),
          "+ Shade a range adds a picked, named, drawn span")

    pg.evaluate("setSelection(['a_fit'])")
    pg.select_option("#f-series-axis", "right"); settle()
    a = disk()["panels"][0]
    check(next(s for s in a["series"] if s["id"] == "a_fit").get("axis") == "right" and "twin" in a
          and "t_a__ylabel2" in svg(), "a line moved to the right axis gets its own axis and label")
    check(pg.locator("[data-eid='a__ylabel2']").count() == 1, "the right label is in the list")
    pg.evaluate("setSelection(['panel:a'])")
    check(pg.is_visible("#twin-fields"), "plot box shows the right-axis fields")
    pg.fill("#f-y2max", "2"); pg.press("#f-y2max", "Enter"); settle()
    check(disk()["panels"][0]["twin"].get("ylim") == [None, 2], "right axis max typed, min stays automatic")

    pg.click("[id='t_a__legend']")
    check(pg.evaluate("selection") == ["a__legend"] and pg.locator("#legend-entries input").count() >= 2,
          "clicking the legend shows a name box per curve")
    pg.fill("[data-series='a_fit']", "my fit"); pg.fill("#f-legend-title", "Key")
    pg.select_option("#f-legend-loc", "lower right"); pg.fill("#f-legend-cols", "2"); pg.press("#f-legend-cols", "Enter"); settle()
    d = disk()["panels"][0]
    check(next(s for s in d["series"] if s["id"] == "a_fit")["label"] == "my fit" and d["legend"]["title"] == "Key"
          and d["legend"]["ncols"] == 2 and d["legend"]["loc"] == "lower right" and not d["legend"].get("xy"),
          "legend: rename an entry, heading, corner, side by side")
    pg.evaluate("setSelection(['a_fit'])")
    check(pg.locator(".ff-halo").count() == 1 and pg.locator("rect.ff-outline").count() == 0,
          "a picked curve glows along its shape (no box)")
    for _ in range(4):
        pg.keyboard.press("Control+z"); settle()

    pg.evaluate("setSelection([])")
    pg.select_option("#f-fig-layout", "3x1"); settle()
    check(disk().get("grid_shape") == [3, 1], "layout: all in one column")

    pg.evaluate("document.activeElement.blur()")
    for _ in range(3):
        pg.keyboard.press("Control+z"); settle()
    d = disk()
    check("grid_shape" not in d and "twin" not in d["panels"][0], "Ctrl+Z undoes layout, range and right axis")
    pg.screenshot(path=_os.path.join(OUT_DIR, "panel_features.png"))
    check(not errs, f"no page errors {errs}")
    br.close()

rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
