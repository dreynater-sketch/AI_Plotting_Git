"""Accounts against the REAL Supabase project: log in, profile, password,
per-user isolation, token refresh. Test users are created confirmed via the
admin API (no email is sent) in a throwaway bucket; everything is deleted
afterwards. Sign-up and password-reset emails are NOT exercised here."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, subprocess, sys, time, urllib.error, urllib.request
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR; os.chdir(ROOT); sys.path.insert(0, ROOT)
env = dict(os.environ)
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
URL, KEY = env["SUPABASE_URL"], env["SUPABASE_SECRET_KEY"]
BUCKET = "figforge-test"; env["FIGFORGE_BUCKET"] = BUCKET
from figforge.project import SupabaseStore
store = SupabaseStore(URL, KEY, BUCKET)


def admin(method, path, body=None):
    req = urllib.request.Request(URL + "/auth/v1/admin" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"apikey": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(); return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"admin {method} {path}: {e.code} {e.read()[:300]}") from None


def drop_bucket():
    if store._call("GET", f"/bucket/{BUCKET}", ok404=True) is not None:
        store._call("POST", f"/bucket/{BUCKET}/empty", {})
        for _ in range(10):
            try: store._call("DELETE", f"/bucket/{BUCKET}"); return "deleted"
            except RuntimeError: time.sleep(1)
        return "could not delete"
    return "absent"


A = {"email": "figforge-test-a@example.com", "password": "test-pass-A-1234", "name": "Test A"}
Bu = {"email": "figforge-test-b@example.com", "password": "test-pass-B-1234", "name": "Test B"}
ids = []
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

drop_bucket()
for u in (A, Bu):
    made = admin("POST", "/users", {"email": u["email"], "password": u["password"], "email_confirm": True,
                                    "user_metadata": {"display_name": u["name"]}})
    u["id"] = made["id"]; ids.append(made["id"])
print("created 2 confirmed test users (no emails sent)")

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ff_real_accounts.log")
srv = subprocess.Popen([sys.executable, "serve.py", "--no-browser", "--port", "8010"], env=env,
                       stdout=subprocess.DEVNULL, stderr=open(LOG, "w"))
B = "http://127.0.0.1:8010"
for _ in range(50):
    try: urllib.request.urlopen(B + "/index.html"); break
    except Exception: time.sleep(0.2)

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        ctx = br.new_context(viewport={"width": 1400, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        loaded = lambda: pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')", timeout=60000)
        settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave && !historyPending", timeout=60000)
        def open_profile():
            if pg.is_hidden("#profile-panel"): pg.click("#btn-account")
        def login(u, pw_=None):
            f = 'form[data-view="login"]'
            pg.fill(f + " [name=email]", u["email"]); pg.fill(f + " [name=password]", pw_ or u["password"])
            pg.click(f + " button[type=submit]")

        pg.goto(B + "/"); pg.wait_for_selector("#auth-screen:not([hidden])")
        check(ctx.request.get(B + "/api/figures").status == 401, "signed out: API locked")
        login(A, "wrong-password-000"); pg.wait_for_function("document.getElementById('auth-msg').textContent.length > 3")
        check("invalid" in pg.inner_text("#auth-msg").lower(), f"wrong password refused ({pg.inner_text('#auth-msg')})")
        t = time.time(); login(A); loaded()
        check(pg.inner_text("#account-name") == "Test A", f"real log-in works, editor loaded ({time.time()-t:.1f}s)")
        objs = store._walk("users")
        check(any(o.startswith(f"users/{A['id']}/qcircle/") for o in objs), "A's space seeded under her real user id")

        tid = pg.evaluate("spec.panels[0].texts[0].id"); s0 = pg.evaluate("spec.panels[0].texts[0].size")
        pg.evaluate(f"setSelection(['{tid}'])"); t = time.time(); pg.click("#f-size-up"); settle()
        check(True, f"edit saved ({time.time()-t:.1f}s)")

        open_profile(); pg.fill("#profile-name", "Test A renamed"); pg.click("#btn-profile-save")
        pg.wait_for_function("document.getElementById('profile-msg').textContent.length > 3")
        check(pg.inner_text("#account-name") == "Test A renamed", f"display name saved to Supabase ({pg.inner_text('#profile-msg')})")
        pg.fill("#profile-pw", "test-pass-A-NEW-99"); pg.fill("#profile-pw2", "test-pass-A-NEW-99")
        pg.click('#profile-password-form button[type=submit]')
        pg.wait_for_function("document.getElementById('profile-msg').textContent.length > 3 && !document.getElementById('profile-msg').textContent.includes('saved')")
        check("updated" in pg.inner_text("#profile-msg"), f"password change ({pg.inner_text('#profile-msg')})")

        # refresh: drop the access-token cookie, keep the refresh token
        rt_before = [c for c in ctx.cookies() if c["name"] == "ff_rt"][0]["value"]
        ctx.clear_cookies(name="ff_at")
        r = ctx.request.get(B + "/api/figures")
        check(r.ok and r.json()["figures"] == ["qcircle"], "no access token + valid refresh token -> refreshed")
        rt_after = [c for c in ctx.cookies() if c["name"] == "ff_rt"][0]["value"]
        check(rt_after != rt_before, "refresh token rotated and re-stored")

        open_profile(); pg.click("#btn-signout"); pg.wait_for_selector("#auth-screen:not([hidden])")
        check(ctx.request.get(B + "/api/figures").status == 401, "signed out: API locked again")
        login(A); pg.wait_for_function("document.getElementById('auth-msg').textContent.length > 3")
        check("invalid" in pg.inner_text("#auth-msg").lower(), "old password rejected by Supabase")
        login(A, "test-pass-A-NEW-99"); loaded()
        check(pg.evaluate("spec.panels[0].texts[0].size") == s0 + 1, "new password works; A's edit is there")

        open_profile(); pg.click("#btn-signout"); pg.wait_for_selector("#auth-screen:not([hidden])")
        login(Bu); loaded()
        check(pg.inner_text("#account-name") == "Test B", "B logs in")
        check(pg.evaluate("spec.panels[0].texts[0].size") == s0, "B doesn't see A's edit")
        check(any(o.startswith(f"users/{Bu['id']}/qcircle/") for o in store._walk("users")), "B has a separate space")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    srv.terminate(); srv.wait()
    tb = [l for l in open(LOG).read().splitlines() if "Traceback" in l or "Error:" in l]
    if tb: print("server errors:", tb[:6])
    for i in ids:
        try: admin("DELETE", f"/users/{i}")
        except RuntimeError as e: print("cleanup:", e)
    print("cleanup: test users deleted; bucket", drop_bucket())
print(len(fails), "failure(s)")
