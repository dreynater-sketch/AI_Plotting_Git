"""Accounts on a deployed preview: locked when signed out, log in with a
temporary confirmed user (admin-created, no email), edit, sign out. The temp
user and its project files are deleted afterwards."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, sys, time, urllib.error, urllib.request
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
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
U = {"email": "figforge-preview-test@example.com", "password": "preview-pass-1234"}
uid = admin("POST", "/users", {"email": U["email"], "password": U["password"], "email_confirm": True,
                               "user_metadata": {"display_name": "Preview Tester"}})["id"]
try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge"); ctx = br.new_context(viewport={"width": 1400, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{H}/?_vercel_share={SHARE}")
        pg.wait_for_selector("#auth-screen:not([hidden])", timeout=60000)
        check(pg.evaluate("spec") is None, "preview: signed-out visitors get the log-in screen")
        for path in ("/api/figures", "/api/figure/qcircle", "/api/png/qcircle", "/api/history/qcircle"):
            check(ctx.request.get(H + path).status == 401, f"preview: {path} is 401 when signed out")
        f = 'form[data-view="login"]'
        pg.fill(f + " [name=email]", U["email"]); pg.fill(f + " [name=password]", U["password"])
        t = time.time(); pg.click(f + " button[type=submit]")
        pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')", timeout=90000)
        check(pg.inner_text("#account-name") == "Preview Tester", f"preview: log-in works ({time.time()-t:.1f}s incl. first-time setup)")
        c = {x["name"]: x for x in ctx.cookies()}
        check(c["ff_at"]["secure"] and c["ff_at"]["httpOnly"], "preview: session cookie is Secure + HttpOnly")
        tid = pg.evaluate("spec.panels[0].texts[0].id")
        pg.evaluate(f"setSelection(['{tid}'])"); t = time.time(); pg.click("#f-size-up")
        pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=60000)
        check(True, f"preview: edit saved ({time.time()-t:.1f}s)")
        pg.click("#btn-account"); pg.click("#btn-signout"); pg.wait_for_selector("#auth-screen:not([hidden])")
        check(ctx.request.get(H + "/api/figures").status == 401, "preview: signed out -> locked")
        pg.screenshot(path=sys.argv[3])
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    keys = store._walk(f"users/{uid}")
    if keys:
        store._call("DELETE", f"/object/figforge", {"prefixes": keys})
    admin("DELETE", f"/users/{uid}")
    print(f"cleanup: temp user deleted, {len(keys)} project files removed")
print(len(fails), "failure(s)")
