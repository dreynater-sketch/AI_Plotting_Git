"""Phase-1 basics: PDF/SVG/PNG download, deleting a project (never the last
one), adding a label or an arrow by hand, and the whole-figure settings
(journal size presets that scale the text, width/height, font)."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, sys, urllib.error, urllib.request
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
from playwright.sync_api import sync_playwright

B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P = "zz_quickwins"; P2 = "zz_quickwins_gone"
for name in (P, P2):
    rmtree(f"figures/{name}")
def post(path, body):
    return urllib.request.urlopen(urllib.request.Request(B + path, data=json.dumps(body).encode(),
                                  method="POST", headers={"Content-Type": "application/json"}))
for name in (P, P2):
    post("/api/project/duplicate", {"from": "qcircle", "name": name})
for name in (P, P2):
    if os.path.exists(f"figures/{name}/history.json"):
        os.remove(f"figures/{name}/history.json")
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))

# --- downloads
for fmt, magic in (("pdf", b"%PDF"), ("svg", b"<?xml"), ("png", b"\x89PNG")):
    r = urllib.request.urlopen(f"{B}/api/export/{P}?format={fmt}")
    body = r.read()
    check(body.startswith(magic) and r.headers["Content-Type"].startswith(("application/pdf", "image/")),
          f"{fmt.upper()} download is a real {fmt.upper()} file ({len(body) // 1024} KB)")
svg = urllib.request.urlopen(f"{B}/api/export/{P}?format=svg").read().decode()
check("<text" in svg, "SVG keeps the words as text (editable in Illustrator/Inkscape)")
try:
    urllib.request.urlopen(f"{B}/api/export/{P}?format=exe"); check(False, "unknown format refused")
except urllib.error.HTTPError as e:
    check(e.code == 400, "unknown format refused")

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge"); pg = br.new_page(viewport={"width": 1400, "height": 850})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project={P}")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)

    # --- add a label / an arrow
    pg.evaluate("setSelection(['panel:b'])")
    pg.click("#btn-add-label")
    lid = pg.evaluate("selection[0]")
    check(lid == "b_label1" and pg.evaluate("document.activeElement.id") == "f-text",
          "+ Label adds a label to the plot you're in, picked, cursor in its text")
    pg.keyboard.type("Nb3Sn edge"); settle()
    t = next(x for x in disk()["panels"][1]["texts"] if x["id"] == "b_label1")
    check(t["text"] == "Nb3Sn edge", "typing names it; saved")
    pg.click("#btn-add-arrow"); settle()
    aid = pg.evaluate("selection[0]")
    arrows = disk()["panels"][1]["arrows"]
    check(aid == "b_arrow1" and any(a["id"] == "b_arrow1" for a in arrows), "+ Arrow adds a picked arrow")
    pg.evaluate("document.activeElement.blur()")
    pg.keyboard.press("Control+z"); settle()
    check(not any(a["id"] == "b_arrow1" for a in disk()["panels"][1].get("arrows", [])), "Ctrl+Z removes it again")

    # --- whole figure: journal 1-column preset scales the text
    pg.evaluate("setSelection([])")
    check(pg.is_visible("#f-fig-preset"), "whole-figure settings show when nothing is picked")
    w0, h0 = disk()["size_in"]
    size0 = disk()["panels"][0]["xlabel"]["size"]
    pg.select_option("#f-fig-preset", "3.4"); settle()
    d = disk()
    check(d["size_in"][0] == 3.4 and abs(d["size_in"][1] - round(3.4 * h0 / w0, 2)) < 0.011,
          f"1-column preset: 3.4 in wide, same proportions ({d['size_in']})")
    check(d["panels"][0]["xlabel"]["size"] < size0, f"text shrinks with it ({size0} -> {d['panels'][0]['xlabel']['size']})")
    pg.fill("#f-fig-h", "3"); pg.press("#f-fig-h", "Enter"); settle()
    check(disk()["size_in"][1] == 3, "height can be typed")
    pg.select_option("#f-fig-font", "serif"); settle()
    check(disk()["rcparams"].get("font.family") == "serif", "font: serif")

    # --- delete a project
    pg.goto(f"{B}/?project={P2}")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    pg.once("dialog", lambda dlg: dlg.accept())
    pg.click("#btn-delete-project")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1' && FIGURE !== 'zz_quickwins_gone'")
    check(not os.path.exists(f"figures/{P2}"), "Delete removes the project and opens another")
    pg.screenshot(path=_os.path.join(OUT_DIR, "quickwins.png"))
    check(not errs, f"no page errors {errs}")
    br.close()

names = json.load(urllib.request.urlopen(f"{B}/api/figures"))["figures"]
check(P2 not in names, "the deleted project is gone from the list")
rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
