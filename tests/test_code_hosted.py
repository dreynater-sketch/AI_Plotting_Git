"""Code editor in hosted mode (mock Supabase): locked when signed out,
per-user edited scripts, run works with data fetched through the session."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, subprocess, sys, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
B, M = "http://127.0.0.1:8010", "http://127.0.0.1:8020"
mock = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_supabase.py"), "8020"])
env = {**os.environ, "SUPABASE_URL": M, "SUPABASE_SECRET_KEY": "sb_secret_test", "FIGFORGE_OPEN_SIGNUP": "1"}
LOG = os.path.join(OUT_DIR, "ff_code_hosted.log")
srv = subprocess.Popen([sys.executable, "serve.py", "--no-browser", "--port", "8010"], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=open(LOG, "w"))
for _ in range(50):
    try: urllib.request.urlopen(B + "/index.html"); break
    except Exception: time.sleep(0.2)

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
get = lambda u: json.loads(urllib.request.urlopen(u).read())

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        errs = []
        def user(name, email):
            ctx = br.new_context(viewport={"width": 1400, "height": 850})
            pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(B + "/code.html?project=qcircle")
            pg.wait_for_selector("#blocker:not([hidden])")
            if name == "Ada":
                check("signed out" in pg.inner_text("#blocker-card"), "signed out: code page shows 'log in' blocker")
                for path in ("/api/script/qcircle", "/api/npz/qcircle", "/api/code/qcircle?edited=1"):
                    check(ctx.request.get(B + path).status == 401, f"signed out: {path} is 401")
            pg.goto(B + "/")
            pg.click('#auth-tabs button[data-view="signup"]')
            f = 'form[data-view="signup"]'
            pg.fill(f + " [name=display_name]", name); pg.fill(f + " [name=email]", email); pg.fill(f + " [name=password]", "long-enough-1")
            pg.click(f + " button[type=submit]"); pg.wait_for_function("document.getElementById('auth-msg').textContent.length > 2")
            link = [m for m in get(M + "/_mailbox") if m["to"] == email][-1]["link"]
            pg.goto(link); pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')")
            pg.goto(B + "/code.html?project=qcircle")
            pg.wait_for_function("typeof cm !== 'undefined' && cm.getValue().includes('import matplotlib')")
            return ctx, pg

        actx, apg = user("Ada", "ada@code.dev")
        check(apg.is_hidden("#blocker"), "signed in: code page loads")
        apg.evaluate("cm.setValue(cm.getValue() + '\\nprint(\"ada was here\")\\n')")
        apg.keyboard.press("Control+s")
        apg.wait_for_function("!document.getElementById('tab').classList.contains('dirty')")
        n = apg.evaluate("runsFinished"); apg.keyboard.press("Control+b")
        apg.wait_for_function(f"runsFinished > {n}", timeout=240000)
        check(apg.inner_text("#panel-state").startswith("Finished") and "ada was here" in apg.inner_text("#console"),
              f"hosted Run works with the session's data ({apg.inner_text('#panel-state')})")
        check("deprecated" not in apg.inner_text("#console"), "no matplotlib deprecation noise in the output")

        bctx, bpg = user("Bob", "bob@code.dev")
        check("ada was here" not in bpg.evaluate("cm.getValue()") and bpg.inner_text("#status-source").startswith("Generated"),
              "Bob gets his own (generated) code, not Ada's edit")
        check(bctx.request.get(B + "/api/code/qcircle?edited=1").status == 404, "Bob has no edited version")
        apg.evaluate("resetToGenerated = resetToGenerated")
        apg.once("dialog", lambda d: d.accept())
        apg.keyboard.press("F1"); apg.keyboard.type("reset"); apg.keyboard.press("Enter")
        apg.wait_for_function("document.getElementById('status-source').textContent.startsWith('Generated')")
        check(actx.request.get(B + "/api/code/qcircle?edited=1").status == 404, "Ada's reset removes her edited copy (storage delete)")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    srv.terminate(); mock.terminate(); srv.wait()
    tb = [l for l in open(LOG).read().splitlines() if "Traceback" in l or "Error:" in l]
    if tb: print("server errors:", tb[:6])
print(len(fails), "failure(s)")
