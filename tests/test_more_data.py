"""Lines from another file, saved code that follows the figure, and the
Change-with-AI talk window -- end to end in the browser (local server)."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, tempfile, time, urllib.request
from _cleanup import rmtree
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR; os.chdir(ROOT)
S = OUT_DIR
B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765")
P = "zz_more_data"
if os.path.exists(f"figures/{P}"): rmtree(f"figures/{P}")

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))


def edit(pg, js):
    """Change the figure in the editor page and wait until the server has it."""
    rev = disk()["rev"]
    pg.evaluate("() => { pushHistory(); " + js + "; scheduleSave(); }")
    for _ in range(150):
        try:
            if disk()["rev"] > rev:
                return
        except (ValueError, OSError):   # caught mid-write (Windows locks it)
            pass
        time.sleep(0.1)
    raise TimeoutError("the figure wasn't saved")


def api(path, body=None):
    req = urllib.request.Request(B + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


first = "t,signal\n" + "".join(f"{i},{i * i}\n" for i in range(10))
st, _ = api("/api/project/from-csv", {"name": P, "csv": first, "filename": "run1.csv", "x": 0, "ys": [1]})
check(st == 200, "a figure from a first file")

# The second file uses the same column names, and reaches further.
second_path = os.path.join(tempfile.mkdtemp(), "run2.csv")
open(second_path, "w").write("t,signal,noise\n" + "".join(f"{i},{200 + i},{i % 3}\n" for i in range(5, 15)))

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    ctx = br.new_context(viewport={"width": 1500, "height": 900})
    pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{B}/?project={P}")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    xlim0 = pg.evaluate("spec.panels[0].xlim")

    # ---------------------------------------------------- lines from a file
    pg.click("#menu-insert"); pg.click("#menu-add-curves")
    check(pg.inner_text("#csv-title") == "Add lines from another file" and not pg.is_visible("#csv-name"),
          "Insert > Lines from another file opens the file dialog, without a project name")
    pg.set_input_files("#csv-file", second_path)
    pg.wait_for_selector("#csv-pick:not([hidden])")
    pg.check('input[name="csv-y"][value="1"]')
    pg.screenshot(path=S + "/add_lines_dialog.png")
    pg.click("#csv-create")
    pg.wait_for_function("document.getElementById('csv-modal').hidden", timeout=30000)
    p = pg.evaluate("spec.panels[0]")
    labels = [s["label"] for s in p["series"]]
    check(labels == ["signal", "signal (run2)"], f"the new line joins the plot, named after its file ({labels})")
    check(p["xlim"][1] > xlim0[1] and p["ylim"][1] > 200, f"the plot grew to show it ({p['xlim']}, {p['ylim']})")
    check(p.get("legend") is not None and pg.evaluate("!!svgEl.querySelector('[id=\"t_a__legend\"]')"),
          "a legend appears for two lines")
    check(len(disk()["panels"][0]["series"]) == 2 and disk()["source"]["panels"][0]["more"][0]["ys"] == ["signal (run2)"],
          "saved, and Start over would keep it")
    pg.screenshot(path=S + "/add_lines_after.png")

    pg.keyboard.press("Control+z")
    pg.wait_for_function("spec.panels[0].series.length === 1")
    check(True, "Ctrl+Z takes the new line away")
    pg.keyboard.press("Control+y")
    pg.wait_for_function("spec.panels[0].series.length === 2")
    for _ in range(150):
        if pg.evaluate("!savePending && !inFlight") and len(disk()["panels"][0]["series"]) == 2:
            break
        time.sleep(0.1)

    # ----------------------------------------- saved code follows the figure
    st, d = api(f"/api/script/{P}")
    mine = d["generated"].replace("plt.show()", "plt.show()\nprint('my own line')")
    st, saved = api(f"/api/script/{P}", {"code": mine, "base_rev": d["rev"]})
    check(st == 200 and saved["code"] == mine, "my edited figure.py is saved")

    # Change the x-axis label in the figure editor.
    edit(pg, "spec.panels[0].xlabel.text = 'time (s)'")
    st, d = api(f"/api/script/{P}")
    code = d["custom"]["code"]
    check("print('my own line')" in code and "'time (s)'" in code and d["custom"]["base_rev"] == d["rev"],
          "my saved code now has the figure's change and keeps my own line")
    check(d["custom"]["merged"]["conflicts"] == [], "nothing clashed")

    # Both change the same line: mine wins, and it's reported.
    # (a note on the line: the figure is unchanged by it, but the line is mine)
    line = next(l for l in code.splitlines() if l.startswith("ax.set_xlabel('time (s)'"))
    mine = code.replace(line, line + "  # my note")
    api(f"/api/script/{P}", {"code": mine, "base_rev": d["rev"]})
    edit(pg, "spec.panels[0].xlabel.text = 'figure time'")
    st, d = api(f"/api/script/{P}")
    check("# my note" in d["custom"]["code"] and "'figure time'" not in d["custom"]["code"]
          and len(d["custom"]["merged"]["conflicts"]) == 1, "where both changed a line, mine is kept and reported")

    # Saving code from an older version merges the figure's newer changes in.
    st, d = api(f"/api/script/{P}")
    old_rev, old_code = d["rev"], d["custom"]["code"]
    edit(pg, "spec.panels[0].ylabel.text = 'counts'")
    st, saved = api(f"/api/script/{P}", {"code": old_code + "\nprint('later')\n", "base_rev": old_rev})
    check(st == 200 and "'counts'" in saved["code"] and "print('later')" in saved["code"]
          and disk()["panels"][0]["ylabel"]["text"] == "counts",
          "saving code from before a figure change doesn't undo that change")
    # (the code tab would tell this page; here it was the test, so reload)
    pg.reload(); pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")

    # The code tab, open and untouched, follows the figure live.
    pg.click("#btn-py")
    with ctx.expect_page() as np_:
        pg.click("#py-view")
    code_pg = np_.value; code_pg.on("pageerror", lambda e: errs.append("code: " + str(e)))
    code_pg.wait_for_function("typeof cm !== 'undefined' && cm.getValue().includes('later')")
    pg.bring_to_front()
    edit(pg, "spec.panels[0].title.text = 'Live title'")
    code_pg.wait_for_function("cm.getValue().includes('Live title')", timeout=15000)
    check("print('later')" in code_pg.evaluate("cm.getValue()") and code_pg.is_visible("#banner"),
          "the open code tab shows the figure's change in my code, and says so")
    code_pg.screenshot(path=S + "/code_follows_figure.png")
    # With unsaved typing it doesn't replace anything, but warns.
    code_pg.evaluate("cm.setValue(cm.getValue() + '\\n# typing')")
    edit(pg, "spec.panels[0].title.text = 'Another title'")
    code_pg.wait_for_function("document.getElementById('banner').textContent.includes('When you save')", timeout=15000)
    check("# typing" in code_pg.evaluate("cm.getValue()"), "unsaved typing is never replaced")
    code_pg.keyboard.press("Control+s")
    code_pg.wait_for_function("cm.getValue().includes('Another title') && cm.getValue().includes('# typing')",
                              timeout=15000)
    check(disk()["panels"][0]["title"]["text"] == "Another title", "…and saving adds the figure's change in")
    code_pg.close()

    # ------------------------------------------------------- the AI talk
    json.dump({"version": 2, "idea": "Make it red", "mode": "edit", "done": True, "created": True,
               "messages": [{"role": "user", "content": [{"type": "text", "text": "Make it red"}]}],
               "cost_usd": 0.1, "steps": 2, "tables": {}, "reply": "Made the line red.",
               "chat": [{"role": "user", "text": "Make it red"},
                        {"role": "assistant", "text": "Made the line red.", "steps": 2, "cost_usd": 0.1}]},
              open(f"figures/{P}/assistant.json", "w"))
    st, d = api(f"/api/assistant/chat/{P}")
    check(d["can_continue"] and len(d["turns"]) == 2 and not d["running"], "the server hands back the talk")
    pg.evaluate("openAiEdit()")
    pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 2")
    check(pg.inner_text("#ai-chat .chat-msg.you") == "Make it red"
          and "Made the line red." in pg.inner_text("#ai-chat .chat-msg.ai")
          and pg.inner_text("#ai-edit-ask") == "Anything else?" and pg.is_visible("#ai-edit-new"),
          "the talk window shows the talk so far and asks for more")
    check(pg.evaluate("aiChatGoesOn"), "the next message would go on with it")
    pg.screenshot(path=S + "/ai_talk.png")
    pg.click("#ai-edit-new")
    check(pg.evaluate("document.querySelectorAll('#ai-chat .chat-msg').length") == 0
          and not pg.evaluate("aiChatGoesOn") and pg.inner_text("#ai-edit-ask") == "What should change?",
          "Start a new talk clears it")
    pg.click("#ai-edit-done")

    check(not errs, f"no page errors {errs}")
    br.close()

rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
