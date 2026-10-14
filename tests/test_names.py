"""Project names with spaces: Save as and Rename take them (extra spaces are
tidied away), the figure opens, saves and shows its name; names the folder
or link can't hold are refused."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import json, os, sys, urllib.error, urllib.request
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
from playwright.sync_api import sync_playwright

B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765")
P, COPY, RENAMED = "zz_names", "zz my plot", "zz my plot 2"
for n in (P, COPY, RENAMED):
    rmtree(f"figures/{n}")
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

def post(path, body):
    try:
        r = urllib.request.urlopen(urllib.request.Request(B + path, data=json.dumps(body).encode(),
                                   method="POST", headers={"Content-Type": "application/json"}))
        return r.status
    except urllib.error.HTTPError as e:
        return e.code

post("/api/project/duplicate", {"from": "qcircle", "name": P})
try:
    for bad in (" zz lead", "zz trail ", "zz  two", "zz/slash", "zz.dot"):
        check(post("/api/project/duplicate", {"from": P, "name": bad}) == 400, f"refused: {bad!r}")

    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        pg = br.new_page(viewport={"width": 1400, "height": 850}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        ready = lambda: pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
        opened = lambda n: pg.wait_for_function(f"typeof FIGURE !== 'undefined' && FIGURE === {json.dumps(n)}"
                                                " && document.getElementById('status').dataset.ready === '1'")
        pg.goto(f"{B}/?project={P}"); ready()

        # Save as, typed with extra spaces: tidied to single ones
        pg.once("dialog", lambda d: d.accept("  zz   my plot "))
        pg.click("#menu-file"); pg.click("#btn-saveas"); opened(COPY)
        check(os.path.exists(f"figures/{COPY}/spec.json") and pg.input_value("#project-select") == COPY,
              "Save as \"  zz   my plot \" makes \"zz my plot\" and opens it")
        check("zz my plot" in pg.title(), f"…its name shows in the tab ({pg.title()})")

        # an edit saves into the folder with the space
        tid = pg.evaluate("spec.panels[0].texts[0].id"); s0 = pg.evaluate("spec.panels[0].texts[0].size")
        pg.evaluate(f"setSelection(['{tid}'])"); pg.click("#f-size-up")
        pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
        disk = json.load(open(f"figures/{COPY}/spec.json", encoding="utf-8"))
        check(disk["panels"][0]["texts"][0]["size"] == s0 + 1, "an edit saves")

        # Rename to another name with spaces; reload keeps it
        pg.once("dialog", lambda d: d.accept(RENAMED))
        pg.click("#menu-file"); pg.click("#btn-rename")
        opened(RENAMED)
        pg.reload(); ready()
        check(pg.evaluate("FIGURE") == RENAMED and not os.path.exists(f"figures/{COPY}")
              and pg.evaluate("spec.panels[0].texts[0].size") == s0 + 1,
              "Rename to \"zz my plot 2\" works and stays after a reload")

        # a bad name in the box is refused with a plain message
        pg.once("dialog", lambda d: d.accept("zz bad/name"))
        pg.click("#menu-file"); pg.click("#btn-rename")
        check("spaces" in pg.inner_text("#status") and pg.evaluate("FIGURE") == RENAMED,
              "a name with / is refused and says what's allowed")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    for n in (P, COPY, RENAMED):
        rmtree(f"figures/{n}")
print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
