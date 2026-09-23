import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, urllib.request
from playwright.sync_api import sync_playwright
os.chdir(ROOT_DIR)
B=_os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P="zz_pad"
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
    ev=pg.evaluate
    wh=lambda i: ev(f"(()=>{{const b=groupFor('{i}').querySelector('g[id^=\"patch\"] path').getBBox();return [b.width,b.height]}})()")

    ev("setSelection(['c_QL'])")
    check(ev("$('f-box-pad').value")=="0.25", "shows current padding 0.25")
    w0=wh("c_QL")
    pg.fill("#f-box-pad","0.05"); pg.press("#f-box-pad","Enter"); settle()
    w1=wh("c_QL")
    check(txt("c_QL")["bbox"]["boxstyle"]=="round,pad=0.05", "saved round,pad=0.05")
    check(w1[0]<w0[0] and w1[1]<w0[1], f"box actually smaller {w0} -> {w1}")
    ec=txt("c_QL")["bbox"]["ec"]

    # mixed selection: all labels, only boxed ones change; checkbox keeps existing boxes
    ev("setSelection(allElements(spec).filter(e=>e.kind==='text').map(e=>e.id))")
    check(ev("!$('box-colors').hidden"), "box settings visible for mixed selection")
    check(ev("$('f-box-pad').placeholder")=="mixed", "mixed padding shown as 'mixed'")
    pg.fill("#f-box-pad","0.1"); pg.press("#f-box-pad","Enter"); settle()
    d=json.load(open(f"figures/{P}/spec.json",encoding="utf-8"))
    ts=[t for p in d["panels"] for t in p["texts"]]
    check(all("pad=0.1" in t["bbox"]["boxstyle"] for t in ts if t.get("bbox")), "every boxed label now pad=0.1")
    check(any(not t.get("bbox") for t in ts), "unboxed labels stayed unboxed")
    check(txt("c_QL")["bbox"]["ec"]==ec, "border color untouched by padding change")

    nb=ev("allElements(spec).find(e=>e.kind==='text' && !e.obj.bbox).id"); print('unboxed label:', nb)
    ev(f"setSelection(['{nb}'])"); pg.check("#f-box"); settle()
    check(txt(nb)["bbox"]["boxstyle"]=="round,pad=0.1", "new box defaults to pad=0.1")
    ev("document.activeElement.blur()"); pg.keyboard.press("Control+z"); settle()
    check(not txt(nb).get("bbox"), "undo removes the new box")
    check(not errs, f"no page errors {errs}")
    br.close()
shutil.rmtree(f"figures/{P}")
print(len(fails),"failure(s)")
