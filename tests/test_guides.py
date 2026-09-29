"""Guide lines (axhline/axvline) and the grey lines through zero can be
clicked, listed, restyled, deleted, brought back and undone."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, urllib.request
from playwright.sync_api import sync_playwright
os.chdir(ROOT_DIR)
B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P = "zz_guides"
if os.path.exists(f"figures/{P}"): shutil.rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B + "/api/project/duplicate",
    data=json.dumps({"from": "qcircle", "name": P}).encode(), method="POST"))
if os.path.exists(f"figures/{P}/history.json"): os.remove(f"figures/{P}/history.json")
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
def panel(pid):
    return next(p for p in json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))["panels"]
                if p["id"] == pid)

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge"); pg = br.new_page(viewport={"width": 1500, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project={P}")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=20000)
    ev = pg.evaluate
    box = lambda gid: ev("gid => { const r = document.getElementById(gid).getBoundingClientRect();"
                         " return [r.x, r.y, r.width, r.height]; }", gid)

    # Clicking the grey zero line (away from the curve) picks "zero lines".
    x, y, w, h = box("t_a__zero__h")
    pg.mouse.click(x + 30, y + h / 2)
    check(ev("selection") == ["a__zero"], f"click on the zero line picks it ({ev('selection')})")
    check(ev("$('insp-kind').textContent") == "Zero lines", "side panel names it 'Zero lines'")

    # A guide line: click high up, where the curve isn't.
    x, y, w, h = box("t_c__vline_1")
    pg.mouse.click(x + w / 2, y + h - 20)
    check(ev("selection") == ["c__vline_1"], f"click on the f0 guide line picks it ({ev('selection')})")
    rows = ev("[...document.querySelectorAll('#element-list button')].map(b => b.textContent)")
    check(any("Up-down line at x = 0" in r for r in rows), "listed as 'Up-down line at x = 0'")
    check(any("Lines through zero" in r for r in rows), "zero lines listed")
    check(ev("document.querySelector('#element-list button.on')?.dataset.eid") == "c__vline_1",
          "its list row is highlighted")

    # Restyle it, then undo.
    pg.fill("#f-guide-lw", "3"); pg.press("#f-guide-lw", "Enter"); settle()
    check(panel("c")["vlines"][1]["lw"] == 3, "thickness saved")
    pg.select_option("#f-guide-ls", "--"); settle()
    check(panel("c")["vlines"][1]["ls"] == "--", "dash style saved")
    ev("document.activeElement.blur()")
    pg.keyboard.press("Control+z"); settle()
    check(panel("c")["vlines"][1]["ls"] == ":", "Ctrl+Z undoes the style change")

    # Move it by typing a position.
    pg.fill("#f-guide-pos", "2.5"); pg.press("#f-guide-pos", "Enter"); settle()
    check(panel("c")["vlines"][1]["x"] == 2.5, "position saved")

    # Delete: guide gone; zero lines hidden, then back from the plot box.
    ev("setSelection(['c__vline_1'])"); pg.click("#btn-delete"); settle()
    check(len(panel("c")["vlines"]) == 2, "guide line deleted")
    ev("setSelection(['a__zero'])"); pg.click("#btn-delete"); settle()
    check(panel("a")["zero_lines"] is False, "zero lines turned off")
    ev("setSelection(['panel:a'])")
    check(not ev("$('f-zero').checked"), "plot box shows 'Lines through zero' unticked")
    pg.check("#f-zero"); settle()
    check(panel("a")["zero_lines"] is True, "and ticking it brings them back")

    # Code round-trip still works: the generated figure.py is unchanged in shape.
    code = urllib.request.urlopen(f"{B}/api/code/{P}").read().decode()
    check("ax.axhline(0, color='0.88', lw=0.7)" in code, "figure.py still draws the zero lines")

    pg.screenshot(path=_os.path.join(OUT_DIR, "guides.png"))
    check(not errs, f"no page errors {errs}")
    br.close()
shutil.rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
