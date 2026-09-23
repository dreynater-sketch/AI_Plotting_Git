"""New figure from a CSV, end to end in the browser (local server)."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, tempfile
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR; os.chdir(ROOT)
S = OUT_DIR
B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765")
NAME = "zz_growth"
if os.path.exists(f"figures/{NAME}"): shutil.rmtree(f"figures/{NAME}")

csv_path = os.path.join(tempfile.mkdtemp(), "growth curve.csv")
rows = ["# plate reader export", "time (h);OD600 wt;OD600 mutant;label"]
for i in range(40):
    t = i * 0.5
    wt = 0.05 * 2 ** (t / 2) / (1 + 0.05 * (2 ** (t / 2) - 1) / 1.5)
    mut = "" if i == 17 else f"{0.6 * wt:.4f}".replace(".", ",")
    rows.append(f"{t:.1f};{wt:.4f}".replace(".", ",") + f";{mut};s{i}")
open(csv_path, "w", encoding="utf-8").write("\n".join(rows) + "\n")

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
disk = lambda: json.load(open(f"figures/{NAME}/spec.json", encoding="utf-8"))

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    ctx = br.new_context(viewport={"width": 1500, "height": 900}, accept_downloads=True)
    pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project=qcircle")
    pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")

    pg.click("#btn-new")
    check(pg.is_visible("#csv-modal"), "New from CSV opens the dialog")
    pg.set_input_files("#csv-file", csv_path)
    pg.wait_for_selector("#csv-pick:not([hidden])")
    summary = pg.inner_text("#csv-summary")
    check("40 rows" in summary and "semicolon" in summary, f"file read: {summary[:60]}")
    names = pg.eval_on_selector_all("#csv-cols tr td:first-child", "els => els.map(e => e.textContent)")
    check(names == ["time (h)", "OD600 wt", "OD600 mutant", "label"], f"columns listed: {names}")
    check(pg.is_disabled('input[name="csv-x"][value="3"]'), "text column can't be chosen")
    check(pg.input_value("#csv-name") == "growth_curve", f"project name suggested from the file ({pg.input_value('#csv-name')})")
    pg.check('input[name="csv-y"][value="2"]')           # add the mutant curve too
    pg.select_option("#csv-plot", "line+markers")
    pg.fill("#csv-name", NAME)
    pg.screenshot(path=S + "/csv_dialog.png")
    pg.click("#csv-create")
    pg.wait_for_url(f"**/?project={NAME}", timeout=30000)
    pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")
    p = pg.evaluate("spec.panels[0]")
    check([s["label"] for s in p["series"]] == ["OD600 wt", "OD600 mutant"] and p["xlabel"]["text"] == "time (h)",
          "figure built: two curves, axis named after the x column")
    check(pg.evaluate("svgEl.querySelectorAll('[id^=\"t_a_s\"]').length") == 2, "both curves drawn and clickable")
    check(pg.evaluate("!!svgEl.querySelector('[id=\"t_a__legend\"]')"), "legend shown for 2 curves")
    check(os.path.exists(f"figures/{NAME}/data/source.csv"), "source CSV kept with the project")

    # edit like any figure
    pg.evaluate("setSelection(['a__xlabel'])")
    pg.fill("#f-text", "Time (hours)")
    pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
    check(disk()["panels"][0]["xlabel"]["text"] == "Time (hours)", "editing works on the new figure")

    # data table with a gap (NaN -> blank)
    pg.evaluate("setSelection(['a_s1'])")
    pg.wait_for_function("document.querySelectorAll('#data-table-body tr').length > 30", timeout=15000)
    check(pg.evaluate("document.querySelectorAll('#data-table-body tr').length") == 40, "data table shows the curve's 40 rows")

    # rebuild from the CSV
    pg.click("#btn-rebuild"); pg.click("#btn-rebuild")
    pg.wait_for_function("document.getElementById('status').textContent.startsWith('rebuilt')", timeout=30000)
    check(disk()["panels"][0]["xlabel"]["text"] == "time (h)", "Rebuild regenerates from the saved CSV + column picks")

    # code editor + sync on a one-panel figure
    pg.click("#btn-py")
    with ctx.expect_page() as npg:
        pg.click("#py-view")
    code = npg.value; code.on("pageerror", lambda e: errs.append("code: " + str(e)))
    code.wait_for_function("typeof cm !== 'undefined' && cm.getValue().includes('ax = axes[0, 0]')")
    code.evaluate("cm.setValue(cm.getValue().replace(\"label='OD600 mutant'\", () => \"label='mutant'\"))")
    code.keyboard.press("Control+s")
    code.wait_for_function("document.getElementById('panel-state').textContent.startsWith('Synced')", timeout=30000)
    check(disk()["panels"][0]["series"][1]["label"] == "mutant", "code edit syncs on a one-panel figure")
    n = code.evaluate("runsFinished"); code.keyboard.press("Control+b")
    code.wait_for_function(f"runsFinished > {n}", timeout=240000)
    check(code.inner_text("#panel-state").startswith("Finished"), f"its figure.py runs in the browser ({code.inner_text('#panel-state')})")
    code.close()

    # bad file
    pg.click("#btn-new")
    bad = os.path.join(os.path.dirname(csv_path), "notes.txt")
    open(bad, "w").write("just some words\nand more words\n")
    pg.set_input_files("#csv-file", bad)
    pg.wait_for_function("document.getElementById('csv-msg').textContent.startsWith('Couldn')")
    check("no column" in pg.inner_text("#csv-msg") and pg.is_disabled("#csv-create"), f"a non-numeric file is explained: {pg.inner_text('#csv-msg')}")
    pg.keyboard.press("Escape")
    check(pg.is_hidden("#csv-modal"), "Esc closes the dialog")
    pg.screenshot(path=S + "/csv_figure.png")
    check(not errs, f"no page errors {errs}")
    br.close()

shutil.rmtree(f"figures/{NAME}")
print(len(fails), "failure(s)")
