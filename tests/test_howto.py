""""How this was made" for AI figures. Part 1, no browser and no API key:
assistant.how_made with a fake Claude -- the sandbox commands it collects
(failed ones marked), the request (cheap model, low effort, a JSON schema),
formulas drawn as SVG (a bad one falls back to plain text), and which
sessions can be explained. Part 2, in Edge: the 📖 button and panel in the
editor, then "Open it in the code editor" and ▶ Run on Claude's script."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import json, os, sys, types, urllib.request
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)

from figforge import assistant

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

CODE = """import csv
import numpy as np
import matplotlib.pyplot as plt

# Number of sides, from 3 to 50
n = np.arange(3, 51)
# Area of the polygon inside a circle of radius 1
A_in = 0.5 * n * np.sin(2 * np.pi / n)

# Save the table
with open('ai_table.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['n', 'A_in'])
    w.writerows(zip(n, A_in))

print('rows', len(n), 'last', round(A_in[-1], 4))
plt.plot(n, A_in)
plt.axhline(np.pi, ls='--')
plt.show()
"""
ANSWER = {
    "idea": "Polygons with more and more sides fill up a circle.",
    "math": [{"name": "Inside polygon area", "tex": r"A = \frac{n}{2}\sin\left(\frac{2\pi}{n}\right)",
              "plain": "A = (n/2) sin(2π/n)", "why": "The area of the shape inside the circle."},
             {"name": "Broken", "tex": r"\frac{", "plain": "x = 1", "why": "Can't be drawn."}],
    "code": CODE, "needs_files": False,
    "steps": ["Picked side counts from 3 to 50.", "Worked out each area."],
    "try_it": ["Change 51 to 501 to use more sides."],
}


class Block(types.SimpleNamespace):
    pass


seen = []
def fake_create(**kw):
    seen.append(kw)
    usage = types.SimpleNamespace(input_tokens=3000, output_tokens=1200,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return types.SimpleNamespace(stop_reason="end_turn", usage=usage,
                                 content=[Block(type="text", text=json.dumps(ANSWER))])
assistant._client = lambda: types.SimpleNamespace(beta=types.SimpleNamespace(
    messages=types.SimpleNamespace(create=fake_create)))

state = {"idea": "show a polygon filling a circle", "done": True, "created": True,
         "reply": "Done: the polygon closes in on the circle.",
         "messages": [
             {"role": "user", "content": [{"type": "text", "text": "The user gave no files."}]},
             {"role": "assistant", "content": [
                 {"type": "server_tool_use", "id": "s1", "name": "bash_code_execution",
                  "input": {"command": "python bad.py"}},
                 {"type": "bash_code_execution_tool_result", "tool_use_id": "s1",
                  "content": {"stdout": "", "stderr": "NameError: x", "return_code": 1}},
                 {"type": "server_tool_use", "id": "s2", "name": "bash_code_execution",
                  "input": {"command": "python good.py"}},
                 {"type": "bash_code_execution_tool_result", "tool_use_id": "s2",
                  "content": {"stdout": "ok", "stderr": "", "return_code": 0}}]}]}

cmds = assistant.sandbox_commands(state)
check([c["command"] for c in cmds] == ["python bad.py", "python good.py"]
      and [c["ok"] for c in cmds] == [False, True] and "NameError" in cmds[0]["output"],
      "the sandbox commands are collected in order, failed ones marked")
howto, cost = assistant.how_made(state)
kw = seen[-1]
check(kw["model"] == "claude-haiku-5-5" and kw["output_config"]["effort"] == "low"
      and kw["output_config"]["format"]["type"] == "json_schema" and "fallbacks" not in kw,
      f"one cheap call: Haiku 5.5, low effort, structured output ({kw['model']})")
check("python bad.py" in kw["messages"][0]["content"] and "(failed; fixed later)" in kw["messages"][0]["content"]
      and "show a polygon" in kw["messages"][0]["content"], "Claude is told the idea and the commands")
check(abs(cost - (3000 * 0.10 + 1200 * 0.50) / 1e6) < 1e-9 and howto["cost_usd"] == round(cost, 5),
      f"cost at Haiku prices (${cost:.5f})")
check(howto["math"][0]["svg"] and howto["math"][0]["svg"].lstrip().startswith("<?xml")
      and howto["math"][1]["svg"] is None, "formulas drawn as SVG; one that can't be drawn falls back")
check(howto["commands"] == cmds and howto["code"] == CODE, "the commands and the script are kept")
check(assistant.can_explain(state) and not assistant.can_explain({**state, "mode": "edit"})
      and not assistant.can_explain({**state, "done": False}) and not assistant.can_explain(None),
      "only finished new AI figures can be explained")
seen.clear()
assistant.how_made(state, model="claude-sonnet-5-5")
check(seen[-1].get("fallbacks") == "default", "Sonnet/Opus keep the refusal fallback")

# ---------------------------------------------------------------- in Edge
from playwright.sync_api import sync_playwright
B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P = "zz_howto"
if os.path.exists(f"figures/{P}"): rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B + "/api/project/duplicate",
    data=json.dumps({"from": "qcircle", "name": P}).encode(), method="POST"))
try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        ctx = br.new_context(viewport={"width": 1400, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{B}/?project={P}")
        pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
        pg.wait_for_timeout(500)
        check(pg.is_hidden("#btn-howto"), "no 📖 button on a figure that wasn't made with AI")

        json.dump(howto, open(f"figures/{P}/howto.json", "w", encoding="utf-8"), ensure_ascii=False)
        pg.reload()
        pg.wait_for_selector("#btn-howto:not([hidden])", timeout=15000)
        pg.click("#btn-howto")
        pg.wait_for_selector("#howto-body:not([hidden])")
        check(pg.inner_text("#howto-idea") == ANSWER["idea"], "the idea is shown")
        check(pg.locator("#howto-math img").count() == 1 and "x = 1" in pg.inner_text("#howto-math")
              and "\\frac" not in pg.inner_text("#howto-modal"),
              "one formula drawn as a picture, the other in plain text; no raw LaTeX on screen")
        check(pg.locator("#howto-math img").evaluate("i => i.naturalWidth > 20"), "the formula picture loads")
        check(pg.locator("#howto-steps li").count() == 2 and pg.locator("#howto-try li").count() == 1,
              "steps and try-it list")
        check(pg.inner_text("#howto-code") == CODE.rstrip("\n") or pg.inner_text("#howto-code") == CODE,
              "the script is shown")
        check(pg.locator("#howto-commands pre.failed").count() == 1, "the failed sandbox command is marked")
        pg.screenshot(path="tests/_out/howto_panel.png")

        with ctx.expect_page() as new:
            pg.click("#howto-open-code")
        code = new.value
        code.on("pageerror", lambda e: errs.append(str(e)))
        code.wait_for_function("typeof cm !== 'undefined' && cm.getValue().includes('ai_table.csv')")
        check(code.evaluate("cm.getValue()") == CODE, "the code editor opens Claude's script")
        check(code.is_hidden("#btn-save") and "aren’t saved" in code.inner_text("#banner")
              and code.inner_text(".tab-name") == "how_it_was_made.py", "it's a try-it copy: no Save, says so")
        n = code.evaluate("runsFinished"); code.keyboard.press("Control+b")
        code.wait_for_function(f"runsFinished > {n}", timeout=240000)
        check(code.inner_text("#panel-state").startswith("Finished") and "rows 48" in code.inner_text("#console")
              and code.evaluate("document.querySelectorAll('#figures img').length") == 1,
              f"▶ Run works in the browser ({code.inner_text('#panel-state')})")
        code.keyboard.press("Control+s")
        check("isn’t saved" in code.inner_text("#status-msg"), "Ctrl+S explains instead of saving")
        r = urllib.request.urlopen(f"{B}/api/script/{P}")
        check(not json.loads(r.read()).get("custom"), "figure.py was not touched")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
