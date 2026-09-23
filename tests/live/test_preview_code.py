"""Code editor on a deployed Vercel URL, with a temporary confirmed user."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, sys, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR; sys.path.insert(0, ROOT)
H, SHARE = sys.argv[1], sys.argv[2]
env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
URL, KEY = env["SUPABASE_URL"], env["SUPABASE_SECRET_KEY"]
from figforge.project import SupabaseStore
store = SupabaseStore(URL, KEY, "figforge")


def admin(method, path, body=None):
    req = urllib.request.Request(URL + "/auth/v1/admin" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"apikey": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(); return json.loads(raw) if raw else {}


fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
U = {"email": "figforge-code-test@example.com", "password": "code-test-pass-1234"}
uid = admin("POST", "/users", {"email": U["email"], "password": U["password"], "email_confirm": True,
                               "user_metadata": {"display_name": "Code Tester"}})["id"]
try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge"); ctx = br.new_context(viewport={"width": 1400, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{H}/?_vercel_share={SHARE}")
        pg.wait_for_selector("#auth-screen:not([hidden])", timeout=60000)
        f = 'form[data-view="login"]'
        pg.fill(f + " [name=email]", U["email"]); pg.fill(f + " [name=password]", U["password"])
        pg.click(f + " button[type=submit]")
        pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')", timeout=90000)
        pg.click("#btn-py")
        with ctx.expect_page() as newp:
            pg.click("#py-view")
        code = newp.value; code.on("pageerror", lambda e: errs.append("code: " + str(e)))
        code.wait_for_function("typeof cm !== 'undefined' && cm.getValue().includes('import matplotlib')", timeout=60000)
        check(True, "deployed: figure.py menu opens the code editor tab")
        r = ctx.request.get(H + "/pyworker.js")
        check(r.ok and "javascript" in (r.headers.get("content-type") or ""), f"deployed: pyworker.js served as JS ({r.headers.get('content-type')})")
        code.evaluate("cm.setValue(cm.getValue().replace('ax.set_xlim(-40, 40)', () => 'ax.set_xlim(-22, 22)') + '\\nprint(\"hello from vercel\")\\n')")
        code.keyboard.press("Control+s")
        code.wait_for_function("!document.getElementById('tab').classList.contains('dirty')", timeout=30000)
        code.wait_for_function("document.getElementById('panel-state').textContent.startsWith('Synced')", timeout=30000)
        pg.wait_for_function("document.getElementById('status').textContent.includes('updated from your code')", timeout=20000)
        check(pg.evaluate("spec.panels[2].xlim") == [-22, 22], "deployed: saving code updates the figure editor live")
        check(any(k.endswith("qcircle/custom_figure.json") for k in store._walk(f"users/{uid}")), "deployed: Save stores the edited copy in the user's space")
        n = code.evaluate("runsFinished"); t = time.time(); code.keyboard.press("Control+b")
        code.wait_for_function(f"runsFinished > {n}", timeout=240000)
        check(code.inner_text("#panel-state").startswith("Finished") and "hello from vercel" in code.inner_text("#console")
              and code.evaluate("document.querySelectorAll('#figures img').length") == 1,
              f"deployed: Run in the browser works ({code.inner_text('#panel-state')})")
        code.screenshot(path=sys.argv[3])
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    keys = store._walk(f"users/{uid}")
    if keys:
        store._call("DELETE", "/object/figforge", {"prefixes": keys})
    admin("DELETE", f"/users/{uid}")
    print(f"cleanup: temp user deleted, {len(keys)} files removed")
print(len(fails), "failure(s)")
