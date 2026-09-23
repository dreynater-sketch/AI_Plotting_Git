"""The figure.py menu + Sublime-style code editor + in-browser Run, locally."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, subprocess, sys, urllib.request
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR; os.chdir(ROOT)
S = OUT_DIR
B, P = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"), "zz_code"
if os.path.exists(f"figures/{P}"): shutil.rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B + "/api/project/duplicate",
    data=json.dumps({"from": "qcircle", "name": P}).encode(), method="POST"))
open(f"figures/{P}/spec.json", "wb").write(subprocess.check_output(["git", "show", "6a86d26:figures/qcircle/spec.json"]))
for f in ("history.json", "custom_figure.json"):
    if os.path.exists(f"figures/{P}/{f}"): os.remove(f"figures/{P}/{f}")

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
custom = lambda: json.load(open(f"figures/{P}/custom_figure.json", encoding="utf-8")) if os.path.exists(f"figures/{P}/custom_figure.json") else None

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    ctx = br.new_context(viewport={"width": 1500, "height": 900}, accept_downloads=True)
    main = ctx.new_page(); errs = []
    main.on("pageerror", lambda e: errs.append("main: " + str(e)))
    main.goto(f"{B}/?project={P}")
    main.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")

    # --- the figure.py menu
    main.click("#btn-py")
    check(main.is_visible("#py-view") and main.is_visible("#py-download"), "figure.py opens a menu: view / download")
    main.wait_for_timeout(300)
    check(main.is_hidden("#py-download-edited"), "no 'edited version' entry before one is saved")
    with main.expect_download() as d:
        main.click("#py-download")
    code_dl = open(d.value.path(), encoding="utf-8").read()
    check("import matplotlib" in code_dl, "Download figure.py still works")

    main.click("#btn-py")
    with ctx.expect_page() as newp:
        main.click("#py-view")
    pg = newp.value
    pg.on("pageerror", lambda e: errs.append("code: " + str(e)))
    pg.wait_for_function("typeof cm !== 'undefined' && cm.getValue().includes('import matplotlib')")
    check(f"code.html?project={P}" in pg.url, "View & edit opens code.html in a new tab")
    check(pg.evaluate("cm.getValue()") == code_dl, "editor shows the generated figure.py")
    check(pg.inner_text("#status-source") == "Generated from the figure", "status bar: generated")
    check(pg.evaluate("cm.getOption('keyMap')") == "sublime" and pg.evaluate("cm.getOption('theme')") == "monokai",
          "Sublime keymap + Monokai theme")
    painted = pg.evaluate("""(() => { const c = document.getElementById('minimap');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++; return n; })()""")
    check(painted > 500, f"minimap is drawn ({painted} px)")

    # --- Sublime editing keys
    pg.evaluate("cm.focus(); cm.setCursor({line: 0, ch: 0})")
    pg.evaluate("cm.setSelection({line: 20, ch: 0}, {line: 20, ch: 0})")
    pg.evaluate("""(() => { const i = cm.getValue().indexOf('ax.'); const p = cm.posFromIndex(i);
        cm.setSelection(p, {line: p.line, ch: p.ch + 2}); })()""")
    pg.keyboard.press("Control+d"); pg.keyboard.press("Control+d")
    check(pg.evaluate("cm.listSelections().length") == 3 and "3 selection regions" in pg.inner_text("#status-pos"),
          "Ctrl+D adds the next occurrence (multi-cursor), status bar counts them")
    pg.keyboard.press("Escape")
    pg.evaluate("cm.setCursor({line: 30, ch: 0})")
    before = pg.evaluate("cm.getLine(30)")
    pg.keyboard.press("Control+/")
    check(pg.evaluate("cm.getLine(30)").lstrip().startswith("#"), "Ctrl+/ comments the line")
    pg.keyboard.press("Control+z")
    check(pg.evaluate("cm.getLine(30)") == before, "Ctrl+Z undoes it")

    # --- edit + save
    pg.evaluate("cm.setValue(cm.getValue() + '\\nprint(\"hello from figure.py\")\\n')")
    check(pg.evaluate("document.getElementById('tab').classList.contains('dirty')") and pg.title().startswith("●"),
          "unsaved edit: dirty dot on the tab + title")
    pg.keyboard.press("Control+s")
    pg.wait_for_function("!document.getElementById('tab').classList.contains('dirty')")
    c = custom()
    check(c and "hello from figure.py" in c["code"] and c["base_rev"] is not None, f"Ctrl+S saved the edited copy (base rev {c and c['base_rev']})")
    check(json.load(open(f"figures/{P}/spec.json"))["rev"] and "hello" not in open(f"figures/{P}/figure.py").read(),
          "saving code doesn't touch the figure or the generated figure.py")

    # --- run in the browser
    _n = pg.evaluate("runsFinished"); pg.keyboard.press("Control+b")
    pg.wait_for_function(f"runsFinished > {_n}", timeout=240000)
    state = pg.inner_text("#panel-state")
    check(state.startswith("Finished"), f"Run (first, incl. Python download): {state}")
    check("hello from figure.py" in pg.inner_text("#console"), "print() output shown in the panel")
    check(pg.evaluate("document.querySelectorAll('#figures img').length") == 1, "the figure is rendered in the panel")
    w = pg.evaluate("document.querySelector('#figures img').naturalWidth")
    check(w > 1000, f"rendered figure is full size ({w}px wide)")
    pg.screenshot(path=S + "/code_editor.png")

    _n = pg.evaluate("runsFinished"); pg.keyboard.press("Control+b")
    pg.wait_for_function(f"runsFinished > {_n}", timeout=60000)
    check(pg.inner_text('#panel-state').startswith("Finished"), f"second Run (Python already loaded): {pg.inner_text('#panel-state')}")

    # --- Ctrl+B mid-run restarts with the current code
    pg.evaluate("cm.replaceRange('import time\\ntime.sleep(3)\\n', {line: 12, ch: 0})")
    pg.keyboard.press("Control+b")
    pg.wait_for_function("document.getElementById('panel-state').textContent === 'Running…'", timeout=60000)
    pg.evaluate("cm.replaceRange('', {line: 12, ch: 0}, {line: 14, ch: 0})")
    pg.keyboard.press("Control+b")
    pg.wait_for_function("/Finished|Error/.test(document.getElementById('panel-state').textContent)", timeout=240000)
    check(pg.inner_text('#panel-state').startswith("Finished"), "Ctrl+B during a run restarts it with the new code")

    # --- errors point at the line
    print("   after restart: state=", repr(pg.inner_text('#panel-state')), "running=", pg.evaluate("running"), "line12=", repr(pg.evaluate("cm.getLine(12)")), flush=True)
    pg.evaluate("cm.replaceRange('x = 1 / 0\\n', {line: 12, ch: 0})")
    _n = pg.evaluate("runsFinished"); pg.keyboard.press("Control+b")
    try:
        pg.wait_for_function(f"runsFinished > {_n}", timeout=60000)
    except Exception:
        print("   STUCK: state=", repr(pg.inner_text('#panel-state')), "running=", pg.evaluate("running"), "console=", repr(pg.inner_text('#console')[-300:]), flush=True)
        raise
    check(pg.inner_text("#panel-state") == "Error" and "ZeroDivisionError" in pg.inner_text("#console"), "runtime error reported")
    check(pg.evaluate("cm.lineInfo(12).bgClass || ''").find("cm-error-line") >= 0, "the failing line (13) is highlighted")
    pg.evaluate("cm.replaceRange('def broken(:\\n', {line: 12, ch: 0}, {line: 13, ch: 0})")
    _n = pg.evaluate("runsFinished"); pg.keyboard.press("Control+b")
    pg.wait_for_function(f"runsFinished > {_n}", timeout=60000)
    check("SyntaxError" in pg.inner_text("#console") and "cm-error-line" in (pg.evaluate("cm.lineInfo(12).bgClass") or ""),
          "syntax error reported on its line")

    # --- cancel a stuck script
    pg.evaluate("cm.replaceRange('while True:\\n    pass\\n', {line: 12, ch: 0}, {line: 13, ch: 0})")
    pg.keyboard.press("Control+b")
    pg.wait_for_function("document.getElementById('panel-state').textContent === 'Running…'", timeout=60000)
    pg.wait_for_timeout(800)
    pg.click("#btn-run")
    check(pg.inner_text("#panel-state") == "Cancelled", "a stuck script can be cancelled (Cancel button)")
    pg.evaluate("cm.replaceRange('', {line: 12, ch: 0}, {line: 14, ch: 0})")
    _n = pg.evaluate("runsFinished"); pg.keyboard.press("Control+b")
    pg.wait_for_function(f"runsFinished > {_n}", timeout=240000)
    check(pg.inner_text("#panel-state").startswith("Finished"), "Run works again after cancelling")
    pg.keyboard.press("Control+s")
    pg.wait_for_function("!document.getElementById('tab').classList.contains('dirty')")

    # --- main editor now offers the edited version
    main.click("#btn-py"); main.wait_for_selector("#py-download-edited:not([hidden])")
    with main.expect_download() as d:
        main.click("#py-download-edited")
    check("hello from figure.py" in open(d.value.path(), encoding="utf-8").read(), "main menu: 'Download my edited version'")

    # --- figure changes after the code was forked -> banner
    spec = json.load(open(f"figures/{P}/spec.json"))
    urllib.request.urlopen(urllib.request.Request(f"{B}/api/figure/{P}", data=json.dumps({"spec": spec}).encode(),
                                                  headers={"Content-Type": "application/json"}, method="POST"))
    pg.reload(); pg.wait_for_function("typeof cm !== 'undefined' && cm.getValue().length > 100")
    check(pg.is_visible("#banner") and "rev" in pg.inner_text("#banner"), "banner: figure changed since this code was saved")

    # --- command palette: reset
    pg.keyboard.press("Control+Shift+p")
    check(pg.is_visible("#palette"), "Ctrl+Shift+P opens the command palette")
    pg.keyboard.press("Escape"); pg.keyboard.press("F1")
    check(pg.is_visible("#palette"), "F1 opens it too")
    pg.keyboard.type("rst gen")
    check("Reset to Generated" in pg.inner_text("#palette-list li.on"), "fuzzy match finds 'Reset to Generated Code'")
    pg.once("dialog", lambda dlg: dlg.accept())
    pg.keyboard.press("Enter")
    pg.wait_for_function("document.getElementById('status-source').textContent.startsWith('Generated')")
    check(custom() is None and "hello" not in pg.evaluate("cm.getValue()"), "reset: edited copy removed, generated code back")

    with pg.expect_download() as d:
        pg.click("#btn-download")
    check(d.value.suggested_filename == f"{P}_figure.py", "Download button saves the editor's content")
    check(not errs, f"no page errors {errs}")
    br.close()

shutil.rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
