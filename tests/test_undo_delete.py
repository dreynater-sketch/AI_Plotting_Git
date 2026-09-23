"""Drive the live editor in Edge: delete, undo/redo, coalescing, persistence."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, subprocess, sys, urllib.request
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR
B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765")
P = "zz_t"
os.chdir(ROOT)
if os.path.exists(f"figures/{P}"):
    shutil.rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(
    B + "/api/project/duplicate", data=json.dumps({"from": "qcircle", "name": P}).encode(),
    method="POST"))
import subprocess as _sp
open(f"figures/{P}/spec.json","wb").write(_sp.check_output(["git","show","6a86d26:figures/qcircle/spec.json"]))
if os.path.exists(f"figures/{P}/history.json"): os.remove(f"figures/{P}/history.json")

fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

def disk():
    return json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))

def texts(spec, pid="a"):
    return [t["id"] for t in next(p for p in spec["panels"] if p["id"] == pid)["texts"]]

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    pg = br.new_page(viewport={"width": 1600, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project={P}")
    pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")
    settle = lambda: pg.wait_for_function(
        "!savePending && !inFlight && !needsSave && !historyPending", timeout=20000)
    ev = pg.evaluate

    check(ev("document.getElementById('btn-redo').disabled"), "redo disabled at start")

    # --- delete a text label via real Delete key
    ev("setSelection(['a_R'])")
    check(ev("!document.getElementById('btn-delete').hidden"), "Delete button shown for a label")
    pg.keyboard.press("Delete")
    check("a_R" not in ev("spec.panels[0].texts.map(t=>t.id)"), "a_R removed from spec")
    check(ev("getComputedStyle(svgEl.querySelector('[id=\"t_a_R\"]')).display") == "none",
          "a_R hidden instantly (preview)")
    check(ev("getComputedStyle(svgEl.querySelector('[id=\"t_a_R_arrow\"]')).display") != "none",
          "a_R_arrow NOT hidden (no prefix collision)")
    check(ev("selection.length") == 0, "selection cleared after delete")
    settle()
    check("a_R" not in texts(disk()), "a_R removed on disk")
    check(ev("!svgEl.querySelector('[id=\"t_a_R\"]')"), "a_R gone from real render")

    # --- undo / redo via keyboard
    pg.keyboard.press("Control+z")
    check("a_R" in ev("spec.panels[0].texts.map(t=>t.id)"), "Ctrl+Z restores a_R")
    check(not ev("document.getElementById('btn-redo').disabled"), "redo enabled after undo")
    settle()
    check("a_R" in texts(disk()), "a_R restored on disk")
    check(ev("!!svgEl.querySelector('[id=\"t_a_R\"]')"), "a_R back in real render")
    pg.keyboard.press("Control+y")
    check("a_R" not in ev("spec.panels[0].texts.map(t=>t.id)"), "Ctrl+Y redoes the delete")
    pg.keyboard.press("Control+z")
    pg.keyboard.press("Control+Shift+z")
    check("a_R" not in ev("spec.panels[0].texts.map(t=>t.id)"), "Ctrl+Shift+Z also redoes")
    settle()

    # --- other kinds
    ev("setSelection(['a_meas', 'a_Goff_arrow', 'b__legend', 'c__xlabel'])")
    pg.keyboard.press("Delete")
    settle()
    d = disk()
    pa, pb, pc = d["panels"]
    check("a_meas" not in [s["id"] for s in pa["series"]], "curve deleted")
    check("a_Goff_arrow" not in [a["id"] for a in pa["arrows"]], "arrow deleted")
    check(pb["legend"] is None, "legend deleted")
    check(pc["xlabel"]["text"] == "" and "size" in pc["xlabel"], "x label blanked, style kept")
    check(ev("!svgEl.querySelector('[id=\"t_b__legend\"]')"), "legend gone from render")

    # --- one undo brings all four back (multi-delete is one step)
    pg.keyboard.press("Control+z")
    settle()
    d = disk()
    check("a_meas" in [s["id"] for s in d["panels"][0]["series"]] and d["panels"][1]["legend"]
          and d["panels"][2]["xlabel"]["text"], "one Ctrl+Z restores the whole multi-delete")
    pg.keyboard.press("Control+y")
    settle()

    # --- structure can't be deleted
    ev("setSelection(['panel:a'])")
    check(ev("document.getElementById('btn-delete').hidden"), "Delete button hidden for a panel")
    before = json.dumps(ev("spec.panels"))
    pg.keyboard.press("Delete")
    check(json.dumps(ev("spec.panels")) == before, "panel not deleted")
    check("can’t be deleted" in ev("document.getElementById('status').textContent"),
          "explains why")

    # --- typing coalesces into one undo step; Ctrl+Z in a field is native
    ev("setSelection(['a_C'])")
    n0 = ev("history.length")
    pg.click("#f-text")
    pg.keyboard.press("End")
    pg.keyboard.type("xyz", delay=60)
    check(ev("history.length") == n0 + 1, f"typing 3 chars = 1 undo step ({ev('history.length') - n0})")
    pg.keyboard.press("Control+z")
    check(ev("history.length") == n0 + 1, "Ctrl+Z while typing doesn't trigger app undo")
    settle()

    # --- persistence across reload
    u, r = ev("history.length"), ev("future.length")
    pg.keyboard.press("Escape")
    ev("document.activeElement.blur()")
    pg.keyboard.press("Control+z")
    settle()
    u, r = ev("history.length"), ev("future.length")
    pg.reload()
    pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")
    check((ev("history.length"), ev("future.length")) == (u, r),
          f"undo/redo stacks survive reload ({u}/{r})")
    pg.keyboard.press("Control+y")
    settle()
    check(ev("future.length") == r - 1, "redo works after reload")

    check(not errs, f"no page errors {errs}")
    br.close()

# figure.py must still run after deletions
r = subprocess.run([sys.executable, "figure.py"], cwd=f"figures/{P}",
                   env={**os.environ, "MPLBACKEND": "Agg"}, capture_output=True, text=True)
check(r.returncode == 0, "generated figure.py runs " + r.stderr[-300:])

shutil.rmtree(f"figures/{P}")
print("\n%d failure(s)" % len(fails))
