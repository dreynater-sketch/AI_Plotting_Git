import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, urllib.request
from playwright.sync_api import sync_playwright
os.chdir(ROOT_DIR)
B=_os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P="zz_box"
if os.path.exists(f"figures/{P}"): shutil.rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B+"/api/project/duplicate",
    data=json.dumps({"from":"qcircle","name":P}).encode(), method="POST"))
import subprocess as _sp
open(f"figures/{P}/spec.json","wb").write(_sp.check_output(["git","show","6a86d26:figures/qcircle/spec.json"]))
if os.path.exists(f"figures/{P}/history.json"): os.remove(f"figures/{P}/history.json")
fails=[]
def check(c,m): print(("PASS " if c else "FAIL ")+m); c or fails.append(m)
def txt(i):
    for p in json.load(open(f"figures/{P}/spec.json",encoding="utf-8"))["panels"]:
        for t in p["texts"]:
            if t["id"]==i: return t
with sync_playwright() as pw:
    br=pw.chromium.launch(channel="msedge"); pg=br.new_page(viewport={"width":1500,"height":800})
    errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project={P}")
    pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")
    settle=lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave",timeout=20000)
    ev=pg.evaluate; vis=lambda: ev("!document.getElementById('box-colors').hidden")

    ev("setSelection(['c_f0'])")
    check(not vis(), "no box -> color pickers hidden")
    pg.check("#f-box")
    check(vis(), "box on -> pickers shown")
    check(ev("[$('f-box-fc').value,$('f-box-ec').value]")==["white","black"], "new box: white fill, black border")
    settle(); pg.select_option("#f-box-fc","black")
    fill=ev("getComputedStyle(groupFor('c_f0').querySelector('g[id^=\"patch\"] path')).fill")
    check(fill in ("rgb(0, 0, 0)","black"), f"fill previews instantly ({fill})")
    pg.select_option("#f-box-ec","white")
    settle()
    b=txt("c_f0")["bbox"]; check((b["fc"],b["ec"])==("black","white"), f"saved to disk {b}")
    svg=ev("groupFor('c_f0').querySelector('g[id^=\"patch\"] path').getAttribute('style')")
    check(("fill: #000000" in svg or "fill:" not in svg) and "stroke: #ffffff" in svg, "real matplotlib render has the colors")
    code=open(f"figures/{P}/figure.py",encoding="utf-8").read()
    check("'fc': 'black'" in code and "'ec': 'white'" in code, "figure.py carries the colors")

    ev("setSelection(['c_QL'])")
    check(ev("$('f-box-ec').selectedOptions[0].textContent").lower()=="#e07a3d", "existing orange border shown as-is")
    ev("setSelection(['c_QL','c_f0'])")
    check(ev("$('f-box-fc').selectedOptions[0].textContent")=="mixed", "mixed selection shows 'mixed'")
    pg.select_option("#f-box-fc","white"); settle()
    check(txt("c_QL")["bbox"]["fc"]=="white" and txt("c_f0")["bbox"]["fc"]=="white", "bulk-set fill on both")
    check(txt("c_QL")["bbox"]["ec"].lower()=="#e07a3d", "untouched border kept")
    ev("document.activeElement.blur()")
    pg.keyboard.press("Control+z"); settle()
    check(txt("c_f0")["bbox"]["fc"]=="black", "Ctrl+Z undoes the color change")
    ev("setSelection(['c_f0'])")
    pg.screenshot(path=_os.path.join(OUT_DIR, "box.png"))
    check(not errs, f"no page errors {errs}")
    br.close()
shutil.rmtree(f"figures/{P}")
print(len(fails),"failure(s)")
