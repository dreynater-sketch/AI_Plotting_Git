"""Two-way sync end to end: edit figure.py in the code tab, Save, and the
figure editor tab updates live; Ctrl+Z there undoes it."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, subprocess, urllib.request
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR; os.chdir(ROOT)
S = OUT_DIR
B, P = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"), "zz_sync"
if os.path.exists(f"figures/{P}"): shutil.rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B + "/api/project/duplicate",
    data=json.dumps({"from": "qcircle", "name": P}).encode(), method="POST"))
open(f"figures/{P}/spec.json", "wb").write(subprocess.check_output(["git", "show", "6a86d26:figures/qcircle/spec.json"]))
for f in ("history.json", "custom_figure.json"):
    if os.path.exists(f"figures/{P}/{f}"): os.remove(f"figures/{P}/{f}")

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))
def text(spec, tid):
    return next(t for p in spec["panels"] for t in p["texts"] if t["id"] == tid)

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    ctx = br.new_context(viewport={"width": 1500, "height": 900})
    main = ctx.new_page(); errs = []
    main.on("pageerror", lambda e: errs.append("main: " + str(e)))
    main.goto(f"{B}/?project={P}")
    main.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")
    rev0 = main.evaluate("spec.rev")
    main.click("#btn-py")
    with ctx.expect_page() as np_:
        main.click("#py-view")
    code = np_.value; code.on("pageerror", lambda e: errs.append("code: " + str(e)))
    code.wait_for_function("typeof cm !== 'undefined' && cm.getValue().includes(\"gid='a_R'\")")
    check(True, "generated code carries gid= tags")

    # edit a label's text + size and the (c) x-limits, add a label, add a loop
    code.evaluate(r"""(() => {
        let v = cm.getValue();
        // function replacements: in a replacement *string*, "$'" is a JS pattern
        v = v.replace("'$R=0.075$',\n        fontsize=11", () => "'$R=0.080$',\n        fontsize=16");
        v = v.replace("ax.set_xlim(-40, 40)", () => "ax.set_xlim(-25, 25)");
        v = v.replace("ax = axes[0, 1]", () => "ax = axes[0, 1]\nax.text(0.05, 0.9, 'from code', fontsize=10, color='red', transform=ax.transAxes)");
        v += "\nfor k in range(2):\n    print(k)\n";
        cm.setValue(v);
    })()""")
    code.keyboard.press("Control+s")
    try:
        code.wait_for_function("document.getElementById('panel-state').textContent.startsWith('Synced')", timeout=30000)
    except Exception:
        print("DEBUG state:", repr(code.inner_text('#panel-state')), "| status:", repr(code.inner_text('#status-msg')),
              "| console:", repr(code.inner_text('#console')[:600]), "| has R=0.080:", code.evaluate("cm.getValue().includes('R=0.080')"),
              "| xlim:", code.evaluate("cm.getValue().includes('set_xlim(-25, 25)')"), flush=True)
        raise
    out = code.inner_text("#console")
    check("Applied to the figure" in out and "R=0.080" in out and "added" in out and "from code" in out,
          "Save reports what it applied")
    check("Kept in your code only" in out and "loops" in out, "…and lists the loop it couldn't apply")
    d = disk()
    check(text(d, "a_R")["text"] == "$R=0.080$" and text(d, "a_R")["size"] == 16
          and d["panels"][2]["xlim"] == [-25, 25]
          and any(t["text"] == "from code" for t in d["panels"][1]["texts"]), "figure on disk updated from the code")

    # the figure-editor tab picked it up live
    main.wait_for_function("document.getElementById('status').textContent.includes('updated from your code')", timeout=15000)
    check(main.evaluate("spec.panels[0].texts.find(t => t.id === 'a_R').text") == "$R=0.080$"
          and main.evaluate("spec.panels[2].xlim[0]") == -25, "figure editor tab refreshed live")
    check(main.evaluate("!!svgEl.querySelector('[id^=\"t_b_text\"]')"), "new label drawn and selectable in the figure editor")

    # jump-to-line from the report
    code.click(".console .jump")
    line = code.evaluate("cm.getCursor().line") + 1
    check(code.evaluate(f"cm.getLine({line - 1})").startswith("for k"), f"clicking 'line N' jumps to the loop (line {line})")

    # Ctrl+Z in the figure editor undoes the whole sync
    main.bring_to_front(); main.evaluate("document.activeElement.blur()")
    main.keyboard.press("Control+z")
    main.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
    d = disk()
    check(text(d, "a_R")["text"] == "$R=0.075$" and d["panels"][2]["xlim"] == [-40, 40]
          and not any(t["text"] == "from code" for t in d["panels"][1]["texts"]), "Ctrl+Z in the figure editor undoes the whole sync")

    # the code tab now warns its code no longer matches the figure
    code.bring_to_front(); code.reload()
    code.wait_for_function("typeof cm !== 'undefined' && cm.getValue().length > 100")
    check(code.is_visible("#banner"), "code tab warns the figure moved on (after the undo)")

    # a syntax error saves the code but leaves the figure alone
    before = disk()
    code.evaluate("cm.setValue(cm.getValue() + '\\ndef broken(:\\n')")
    code.keyboard.press("Control+s")
    code.wait_for_function("document.getElementById('panel-state').textContent === 'Not applied'", timeout=30000)
    check("syntax error on line" in code.inner_text("#console") and disk() == before, "syntax error: saved, figure untouched")
    code.screenshot(path=S + "/sync_report.png")
    check(not errs, f"no page errors {errs}")
    br.close()

shutil.rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
