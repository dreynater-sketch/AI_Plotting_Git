"""Changing numbers in the table under the figure: the edit is drawn, saved
in spec.data_edits (the data file keeps the original), written into
figure.py as D["key"][row] = value and read back from it, and undo/redo
work like any other edit."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import copy, json, os, shutil, sys, urllib.request
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
from playwright.sync_api import sync_playwright
from figforge import codegen, codesync, render

B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P = "zz_data_edits"
if os.path.exists(f"figures/{P}"):
    rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B + "/api/project/duplicate",
    data=json.dumps({"from": "qcircle", "name": P}).encode(), method="POST",
    headers={"Content-Type": "application/json"}))
if os.path.exists(f"figures/{P}/history.json"):
    os.remove(f"figures/{P}/history.json")
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge"); pg = br.new_page(viewport={"width": 1400, "height": 850})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project={P}")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
    pg.locator("#element-list button", has_text="circle fit").first.click()
    pg.wait_for_selector("#data-table-body td")
    cell = pg.locator('#data-table-body td[data-i="3"][data-c="1"]')
    original = cell.inner_text()
    cell.click(); pg.keyboard.press("Control+a"); pg.keyboard.type("0.08"); pg.keyboard.press("Enter"); settle()
    key = pg.evaluate("spec.panels[0].series.find(s => s.id === 'a_fit').y")
    check(cell.inner_text() == "0.080000" and "edited" in (cell.get_attribute("class") or ""),
          "the cell shows the new number, marked as changed")
    check(disk().get("data_edits") == {key: {"3": 0.08}}, "saved as a data edit")
    arrays = dict(__import__("numpy").load(f"figures/{P}/data/curves.npz"))
    check(abs(arrays[key][3] - 0.08) > 1e-6, "the data file keeps the original")
    code = urllib.request.urlopen(f"{B}/api/code/{P}").read().decode()
    check(f'D["{key}"][3] = 0.08' in code, "figure.py spells the change out")

    pg.evaluate("document.activeElement.blur()")
    pg.keyboard.press("Control+z"); settle()
    check(cell.inner_text() == original and "data_edits" not in disk(), "Ctrl+Z puts the number back")
    pg.keyboard.press("Control+y"); settle()
    check(cell.inner_text() == "0.080000", "Ctrl+Y redoes it")

    cell.click(); pg.keyboard.press("Delete"); pg.keyboard.press("Escape")
    check(pg.evaluate("!!spec.panels[0].series.find(s => s.id === 'a_fit')"),
          "Delete while typing in a cell doesn't delete the curve")
    cell.click(); pg.keyboard.press("Control+a"); pg.keyboard.type("abc"); pg.keyboard.press("Enter"); settle()
    check(cell.inner_text() == "0.080000", "text that isn't a number is ignored")
    cell.click(); pg.keyboard.press("Control+a"); pg.keyboard.type(original); pg.keyboard.press("Enter"); settle()
    check("data_edits" not in disk(), "typing the original back removes the edit")
    pg.screenshot(path=_os.path.join(OUT_DIR, "data_edits.png"))
    check(not errs, f"no page errors {errs}")
    br.close()

# Code round trip, without the browser.
spec = copy.deepcopy(disk())
spec["data_edits"] = {key: {"3": 0.08, "10": -0.01}}
code = codegen.generate(spec)
back, rep = codesync.apply(code, spec, list(arrays))
check(back.get("data_edits") == spec["data_edits"] and not rep["skipped"], "edits round-trip through code")
back, rep = codesync.apply(code.replace("= 0.08", "= 0.09"), spec, list(arrays))
check(back["data_edits"][key]["3"] == 0.09 and "numbers changed by hand" in rep["changed"],
      "changing the number in code changes the figure")
check(render.apply_data_edits(arrays, spec)[key][10] == -0.01 and arrays[key][10] != -0.01,
      "edits are applied on a copy")
rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
