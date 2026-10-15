"""More kinds of plot: 3-D panels (surfaces that follow a curved edge, meshes,
3-D lines and dots, turned by "view"), round (polar) panels, histograms,
violin plots, arrow fields and hexagon maps. Built from a table, drawn,
exported as plain matplotlib that runs, read back from that code unchanged,
edited by the AI's tools -- and in the editor: a 3-D surface is clicked like
any curve, dragging an empty spot turns the plot, the turn/up boxes and the
height axis work, labels sit across the box, arrows and shading are refused."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import copy, io, json, math, os, subprocess, sys, tempfile
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
import numpy as np

from figforge import codegen, codesync, csvimport, layers, ops, render
from figforge.project import Figure, LocalStore

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

# A critical-surface-like table: each temperature runs up to its own limit
# field (a mesh with a curved edge), plus columns for the flat kinds.
rows = []
for T in np.linspace(2, 17.5, 12):
    bstar = 28 * (1 - (T / 18) ** 1.52)
    for u in np.linspace(0, 1, 10):
        B = 0.05 * (bstar / 0.05) ** u
        rows.append((T, B, 3000 * (1 - (T / 18) ** 2) ** 2 * max(1 - B / bstar, 0.01) ** 2 / math.sqrt(B + 0.3) + 1))
th = np.linspace(0, 2 * np.pi, len(rows))
rng = np.random.default_rng(4)
g1, g2 = rng.normal(0, 1, len(rows)), rng.normal(1, 2, len(rows))
buf = io.StringIO()
buf.write("T,B,J,theta,r,g1,g2\n")
for i, (T, B, J) in enumerate(rows):
    buf.write(f"{T},{B},{J},{th[i]},{1 + math.cos(3 * th[i]) ** 2},{g1[i]},{g2[i]}\n")
TEXT = buf.getvalue()

spec, arrays = csvimport.build_panels("zz_plot_types", TEXT, [
    {"x": "T", "ys": [], "projection": "3d", "yscale": "log", "zscale": "log", "zlabel": "$J_c$",
     "xlabel": "T (K)", "ylabel": "B (T)",
     "layers": [{"kind": "surface", "y": "B", "z": "J", "label": "Nb$_3$Sn"}]},
    {"x": "theta", "ys": ["r"], "projection": "polar"},
    {"x": "", "ys": [], "layers": [{"kind": "hist", "y": "g1"}]},
    {"x": "", "ys": [], "layers": [{"kind": "violin", "columns": ["g1", "g2"]}]},
], "t.csv", rows=2)
a, b, c, d = spec["panels"]
surf = a["series"][0]
check(a["projection"] == "3d" and surf["kind"] == "surface" and arrays[surf["x"]].shape == (12, 10),
      "3-D surface from tidy rows: one mesh row per temperature, each up to its own edge")
check(abs(arrays[surf["y"]][0, -1] - 28 * (1 - (2 / 18) ** 1.52)) < 1e-9,
      "…the mesh keeps each row's own top (not one shared rectangle)")
check(a["zscale"] == "log" and a["zlim"][0] > 0 and a["zlabel"]["text"] == "$J_c$" and surf.get("colorbar"),
      "z axis: log range, label, and one surface gets a colour bar")
check(b["projection"] == "polar" and abs(b["xlim"][1] - 2 * math.pi) < 1e-9 and b["ylim"][0] == 0,
      "polar panel: the angle goes all the way round, distance starts at 0")
check(c["series"][0]["kind"] == "hist" and c["ylim"][0] == 0, "histogram from raw values stands on zero")
check(d["series"][0]["kind"] == "violin" and len(d["series"][0]["groups"]) == 2, "violin plot, one per column")

for bad, why in (
        ([{"x": "T", "ys": ["J"], "projection": "3d", "layers": []}], "lines in a 3-D panel"),
        ([{"x": "T", "ys": [], "layers": [{"kind": "surface", "y": "B", "z": "J"}]}], "a surface on a flat panel")):
    try:
        csvimport.build_panels("t", TEXT, bad)
        check(False, f"refused: {why}")
    except csvimport.CSVError:
        check(True, f"refused: {why}")

two, arr2 = csvimport.build_panels("t", TEXT, [
    {"x": "T", "ys": [], "projection": "3d",
     "layers": [{"kind": "surface", "y": "B", "z": "J", "label": "one"},
                {"kind": "wireframe", "y": "B", "z": "J", "label": "mesh"},
                {"kind": "trisurf", "y": "B", "z": "J", "label": "two"},
                {"kind": "line3d", "y": "B", "z": "J"}, {"kind": "scatter3d", "y": "B", "z": "J", "extra": "g1"}]},
    {"x": "g1", "ys": [], "layers": [{"kind": "hexbin", "y": "g2"},
                                     {"kind": "quiver", "y": "g2", "extra": "g1", "z": "g2"}]}], "t.csv")
kinds = [s["kind"] for s in two["panels"][0]["series"]]
check(kinds == ["surface", "wireframe", "trisurf", "line3d", "scatter3d"], f"every 3-D kind builds ({kinds})")
check(all("cmap" not in s["style"] and "colorbar" not in s for s in two["panels"][0]["series"][::2][:2]),
      "several surfaces: one plain colour each (told apart by the legend)")

# drawn, and every curve is a click target in the editor's picture
for sp, ar in ((spec, arrays), (two, arr2)):
    svg, geom = render.render(sp, ar, preview=True)
    ids = [s["id"] for p in sp["panels"] for s in p["series"]]
    check(all(f'id="t_{i}"' in svg for i in ids), f"every curve can be clicked ({len(ids)})")
check(geom["panels"]["a"]["projection"] == "3d", "the editor is told which plots are 3-D")

# exported code runs and round-trips to the same spec
for name, sp, ar in (("one", spec, arrays), ("two", two, arr2)):
    code = codegen.generate(sp)
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "data"))
        np.savez(os.path.join(tmp, "data", "curves.npz"), **ar)
        open(os.path.join(tmp, "figure.py"), "w", encoding="utf-8").write(code.replace("plt.show()", ""))
        r = subprocess.run([sys.executable, os.path.join(tmp, "figure.py")], capture_output=True, text=True,
                           env={**os.environ, "MPLBACKEND": "Agg"})
    check(r.returncode == 0, f"exported code runs ({name}) {r.stderr[-300:]}")
    new, rep = codesync.apply(code, sp, list(ar))
    check(new == sp and not rep["skipped"], f"code reads back unchanged ({name}) {rep}")
code = codegen.generate(spec)
check("projection='3d'" in code and "projection='polar'" in code and "ax.view_init(" in code
      and '_b["bodies"][0].set_gid(' in code, "code makes 3-D / polar panels, turns the view, tags violins")
turned = code.replace("ax.view_init(elev=25, azim=-60)", "ax.view_init(elev=40, azim=30)") \
             .replace("ax.set_zscale('log')", "")
new, rep = codesync.apply(turned, spec, list(arrays))
check(new["panels"][0]["view"] == {"elev": 40, "azim": 30} and new["panels"][0].get("zscale") == "linear",
      "turning the view or dropping the log height scale in code comes back to the figure")

# the AI's editing tools
new, res, _ = ops.apply(spec, [
    {"name": "set_view", "input": {"panel_id": "a", "elevation": 10, "azimuth": 120}},
    {"name": "set_axis", "input": {"panel_id": "a", "axis": "z", "min": 10, "max": 1e4, "scale": "keep", "tick_size": None}},
    {"name": "set_text", "input": {"element_id": "a__zlabel", "text": "J (A/mm$^2$)"}},
    {"name": "add_span", "input": {"panel_id": "a", "axis": "x", "from": 1, "to": 2, "color": "", "opacity": None, "label": ""}},
    {"name": "add_label", "input": {"panel_id": "b", "text": "x", "x": 1, "y": 1, "coords": "data", "size": None, "color": ""}},
    {"name": "set_view", "input": {"panel_id": "b", "elevation": 10, "azimuth": 0}},
    {"name": "set_axis", "input": {"panel_id": "c", "axis": "z", "min": 1, "max": 2, "scale": "keep", "tick_size": None}},
], arrays)
err = [r["is_error"] for r in res]
check(err == [False, False, False, True, True, True, True], f"AI tools: view, z axis, z label work; flat-only ones refused ({err})")
check(new["panels"][0]["view"] == {"elev": 10, "azim": 120} and new["panels"][0]["zlim"] == [10, 1e4]
      and new["panels"][0]["zlabel"]["text"] == "J (A/mm$^2$)", "…and change the figure")
desc = ops.describe(spec, arrays)["panels"]
check(desc[0]["projection"] == "3d" and "z_axis" in desc[0] and desc[1]["projection"] == "polar",
      "describe_figure says which panels are 3-D / polar")

# ------------------------------------------------------------- the editor
P = "zz_plot_types"
rmtree(f"figures/{P}")
fig = Figure(P, LocalStore())
fig.save_arrays(arrays)
fig.save_source_csv(TEXT)
fig.save_spec(spec)

from playwright.sync_api import sync_playwright
B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765")
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))
try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        pg = br.new_page(viewport={"width": 1400, "height": 900}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{B}/?project={P}")
        pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
        settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
        ev = pg.evaluate

        def at(fx, fy, pid="a"):
            """Screen point at a fraction across / down a panel's box."""
            return ev(f"(() => {{ const g = geometry.panels.{pid}; const r = svgEl.getBoundingClientRect();"
                      f" const k = r.width / geometry.width; return [r.left + (g.bbox[0] + g.bbox[2] * {fx}) * k,"
                      f" r.top + (g.bbox[1] + g.bbox[3] * {fy}) * k]; }})()")

        box = pg.locator("#t_a_l0").bounding_box()
        # the surface's middle (any painted part of it picks it)
        pts = ev("[...document.querySelectorAll('#t_a_l0 path')].slice(40, 41).map((p) => {"
                 " const r = p.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })")
        pg.mouse.click(*(pts[0] if pts else (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)))
        check(ev("selection[0]") == "a_l0" and ev("kindLabel('series', spec.panels[0].series[0])") == "3-D surface",
              f"clicking the 3-D surface picks it ({ev('selection')})")

        # an empty corner of the 3-D plot: a click picks the plot, a drag turns it
        x, y = at(0.04, 0.96)
        pg.mouse.click(x, y)
        check(ev("selection[0]") == "panel:a" and pg.locator("#axes-3d-fields").is_visible()
              and pg.input_value("#f-turn") == "-60" and pg.input_value("#f-tilt") == "25",
              "clicking an empty spot picks the plot; turn / up boxes show the view")
        check(not pg.locator("#btn-add-span").is_visible() and not pg.locator("#f-aspect").is_visible(),
              "…flat-only settings are hidden")
        pg.wait_for_timeout(500)            # the figure re-fits beside the opened settings
        x, y = at(0.04, 0.96)
        pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x + 40, y - 20, steps=5)
        pg.mouse.up(); settle()
        v = disk()["panels"][0].get("view")
        check(v == {"elev": 15, "azim": -80}, f"dragging turns it and saves the view ({v})")
        pg.click("#btn-undo"); settle()
        check(disk()["panels"][0].get("view") in (None, {"elev": 25, "azim": -60}), "Undo turns it back")

        ev("setSelection(['panel:a'])")
        pg.fill("#f-turn", "30"); pg.press("#f-turn", "Enter"); settle()
        pg.fill("#f-zmax", "20000"); pg.press("#f-zmax", "Enter"); settle()
        p0 = disk()["panels"][0]
        check(p0["view"]["azim"] == 30 and p0["zlim"][1] == 20000, "the turn box and height max change the plot")

        pg.mouse.move(*at(0.5, 0.5))
        check("turn" in pg.inner_text("#sb-xy"), "the bottom bar says how to turn it instead of x/y")

        ev("setSelection(['a__zlabel'])")
        check(pg.input_value("#f-text") == "$J_c$" or "J" in pg.inner_text("#sb-picked"), "the height label can be picked")

        ev("setSelection(['panel:a'])")
        pg.click("#menu-insert"); pg.click("#btn-add-label"); settle()
        t = disk()["panels"][0]["texts"][-1]
        check(t["coords"] == "axes" and t["xy"] == [0.5, 0.5], "a new label on a 3-D plot sits in the middle of its box")
        ev("setSelection(['panel:a'])")
        pg.click("#menu-insert"); pg.click("#btn-add-arrow")
        check(not disk()["panels"][0].get("arrows") and "flat" in pg.inner_text("#status"),
              "an arrow is refused on a 3-D plot, saying why")

        ev("setSelection(['c_l0'])")
        check(ev("kindLabel('series', spec.panels[2].series[0])") == "histogram", "a histogram is named as one")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
