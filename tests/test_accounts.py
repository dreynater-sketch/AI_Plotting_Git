"""Accounts, end to end in Edge, against FigForge in hosted mode.

  python test_accounts.py mock   -> FigForge on :8010 backed by mock_supabase on :8020
                                    (sign-up + email links + reset via the mock mailbox)
Reads the mailbox from the mock; never sends a real email."""
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
LOG = os.path.join(OUT_DIR, "ff_accounts.log")
srv = subprocess.Popen([sys.executable, "serve.py", "--no-browser", "--port", "8010"], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=open(LOG, "w"))
for _ in range(50):
    try: urllib.request.urlopen(B + "/index.html"); break
    except Exception: time.sleep(0.2)

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
get = lambda u: json.loads(urllib.request.urlopen(u).read())
def mail(to, kind): return [x for x in get(M + "/_mailbox") if x["to"] == to and x["type"] == kind][-1]["link"]
objects = lambda: get(M + "/_debug")["objects"]

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        ctx = br.new_context(viewport={"width": 1500, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        loaded = lambda: pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')", timeout=30000)
        settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave && !historyPending", timeout=30000)
        msg = lambda: pg.inner_text("#auth-msg")
        def signup(name, email, pw_):
            pg.click('#auth-tabs button[data-view="signup"]')
            f = 'form[data-view="signup"]'
            pg.fill(f + ' [name=display_name]', name); pg.fill(f + ' [name=email]', email); pg.fill(f + ' [name=password]', pw_)
            pg.click(f + ' button[type=submit]'); pg.wait_for_function("document.getElementById('auth-msg').textContent.length > 2")
        def login(email, pw_):
            f = 'form[data-view="login"]'
            pg.fill(f + ' [name=email]', email); pg.fill(f + ' [name=password]', pw_); pg.click(f + ' button[type=submit]')

        # --- signed out
        pg.goto(B + "/")
        pg.wait_for_selector("#auth-screen:not([hidden])")
        check(pg.evaluate("spec") is None, "signed out: auth screen shown, editor not loaded")
        check(ctx.request.get(B + "/api/figures").status == 401, "signed out: /api/figures is 401")
        check(ctx.request.get(B + "/api/png/qcircle").status == 401, "signed out: PNG export is 401")

        # --- validation
        pg.click('#auth-tabs button[data-view="signup"]')
        f = 'form[data-view="signup"]'
        pg.fill(f + ' [name=display_name]', "Ada"); pg.fill(f + ' [name=email]', "ada@test.dev"); pg.fill(f + ' [name=password]', "short")
        check(not pg.evaluate(f"document.querySelector('{f}').checkValidity()"), "browser blocks a short password")
        r = ctx.request.post(B + "/api/auth/signup", data=json.dumps({"email": "ada@test.dev", "password": "short", "display_name": "x"}),
                             headers={"Content-Type": "application/json"})
        check(r.status == 400 and "8 characters" in r.json()["error"], "server refuses a short password too")
        r = ctx.request.post(B + "/api/auth/login", data=json.dumps({"email": "not-an-email", "password": "x"}),
                             headers={"Content-Type": "application/json"})
        check(r.status == 400, "server refuses a malformed email")

        # --- sign up -> confirmation email -> can't log in yet
        signup("Ada", "ada@test.dev", "correct-horse-1")
        check("confirmation link" in msg(), "sign-up asks to confirm by email")
        login("ada@test.dev", "correct-horse-1"); pg.wait_for_function("document.getElementById('auth-msg').textContent.includes('confirm')")
        check("not confirmed" in msg().lower(), f"log-in before confirming refused ({msg()})")

        # --- confirmation link logs in + seeds a private project space
        pg.goto(mail("ada@test.dev", "signup")); loaded()
        check("access_token" not in pg.url, "tokens scrubbed from the address bar")
        check(pg.inner_text("#account-name") == "Ada", "signed in as Ada (display name in header)")
        ada = [o for o in objects() if o.startswith("users/")]
        check(ada and all(o.split("/")[1] == ada[0].split("/")[1] for o in ada) and any(o.endswith("qcircle/spec.json") for o in ada),
              "Ada's projects seeded under users/<her id>/")
        check(pg.inner_text("#storage-badge").lower() == "online", "badge says online")
        cookies = {c["name"]: c for c in ctx.cookies()}
        check(cookies["ff_at"]["httpOnly"] and cookies["ff_rt"]["httpOnly"], "session cookies are HttpOnly")
        check(pg.evaluate("document.cookie").find("ff_") == -1, "page scripts can't read the session")

        # --- Ada edits her figure
        tid = pg.evaluate("spec.panels[0].texts[0].id"); s0 = pg.evaluate("spec.panels[0].texts[0].size")
        pg.evaluate(f"setSelection(['{tid}'])"); pg.click("#f-size-up"); settle()

        # --- profile: display name
        pg.click("#btn-account"); pg.fill("#profile-name", "Ada L."); pg.click("#btn-profile-save")
        pg.wait_for_function("document.getElementById('profile-msg').textContent.includes('saved')")
        check(pg.inner_text("#account-name") == "Ada L.", "display name changed")
        pg.reload(); loaded()
        check(pg.inner_text("#account-name") == "Ada L." and pg.evaluate("spec.panels[0].texts[0].size") == s0 + 1,
              "name and edit survive reload")

        # --- profile: password change
        pg.click("#btn-account"); pg.fill("#profile-pw", "new-horse-22"); pg.fill("#profile-pw2", "new-horse-2X")
        pg.click('#profile-password-form button[type=submit]')
        pg.wait_for_function("document.getElementById('profile-msg').textContent.includes('match')")
        check(True, "mismatched new passwords refused")
        pg.fill("#profile-pw2", "new-horse-22"); pg.click('#profile-password-form button[type=submit]')
        pg.wait_for_function("document.getElementById('profile-msg').textContent.includes('updated')")
        check(True, "password updated")

        # --- sign out
        pg.click("#btn-signout"); pg.wait_for_selector("#auth-screen:not([hidden])")
        check(ctx.request.get(B + "/api/figures").status == 401, "signed out again: API locked")
        login("ada@test.dev", "correct-horse-1"); pg.wait_for_function("document.getElementById('auth-msg').textContent.includes('Invalid')")
        check(True, "old password no longer works")
        login("ada@test.dev", "new-horse-22"); loaded()
        check(pg.inner_text("#account-name") == "Ada L.", "new password logs in")

        # --- a second user sees only their own projects
        pg.click("#btn-account"); pg.click("#btn-signout"); pg.wait_for_selector("#auth-screen:not([hidden])")
        signup("Bob", "bob@test.dev", "bob-password-1")
        pg.goto(mail("bob@test.dev", "signup")); loaded()
        check(pg.inner_text("#account-name") == "Bob", "Bob signed in")
        check(pg.evaluate("spec.panels[0].texts[0].size") == s0, "Bob does not see Ada's edit (isolated)")
        pg.evaluate("document.activeElement.blur()")
        pg.click("#btn-saveas") if False else None
        r = ctx.request.post(B + "/api/project/duplicate", data=json.dumps({"from": "qcircle", "name": "bobs"}),
                             headers={"Content-Type": "application/json"})
        check(r.ok and r.json()["figures"] == ["bobs", "qcircle"], "Bob's Save as lands in his space")
        ada_id = ada[0].split("/")[1]
        check(not any(o.startswith(f"users/{ada_id}/bobs") for o in objects()), "…not in Ada's")

        # --- expired access token: refreshed transparently
        get(M + "/_expire")
        pg.reload(); loaded()
        check(pg.inner_text("#account-name") == "Bob", "expired access token refreshed silently")

        # --- session revoked mid-edit: page sends you to log in
        get(M + "/_revoke")
        tid = pg.evaluate("spec.panels[0].texts[0].id")
        pg.evaluate(f"setSelection(['{tid}'])"); pg.click("#f-size-up")
        pg.wait_for_selector("#auth-screen:not([hidden])", timeout=15000)
        check("session ended" in msg(), "revoked session -> log-in screen with a message")

        # --- forgot password -> reset link -> set new password
        pg.click('form[data-view="login"] [data-goto="forgot"]')
        pg.fill('form[data-view="forgot"] [name=email]', "bob@test.dev"); pg.click('form[data-view="forgot"] button[type=submit]')
        pg.wait_for_function("document.getElementById('auth-msg').textContent.includes('reset link')")
        check(True, "reset email requested")
        pg.goto(mail("bob@test.dev", "recovery"))
        pg.wait_for_selector('form[data-view="reset"]:not([hidden])')
        check(True, "reset link opens the set-password form")
        pg.fill('form[data-view="reset"] [name=password]', "bob-reset-pass-9")
        pg.fill('form[data-view="reset"] [name=password2]', "bob-reset-pass-9")
        pg.click('form[data-view="reset"] button[type=submit]'); loaded()
        check(pg.inner_text("#account-name") == "Bob", "new password set, straight into the editor")
        log = []
        pg.on("request", lambda r: log.append(("REQ", r.method, r.url[-40:])) if "/api/" in r.url or r.resource_type == "document" else None)
        pg.on("response", lambda r: log.append(("RES", r.status, r.url[-40:])) if "/api/" in r.url or r.request.resource_type == "document" else None)
        pg.on("requestfailed", lambda r: log.append(("FAILED", r.url[-40:], r.failure)))
        pg.click("#btn-account"); pg.click("#btn-signout"); pg.wait_for_selector("#auth-screen:not([hidden])")
        log.append(("--- login now",))
        login("bob@test.dev", "bob-reset-pass-9")
        try: loaded()
        except Exception:
            print("DEBUG auth-msg:", repr(msg()), "| url:", pg.url); [print("   ", x) for x in log]; raise
        check(True, "log in with the reset password")

        # --- expired / broken email link
        pg.goto(B + "/#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired")
        pg.wait_for_selector("#auth-screen:not([hidden])")
        check("expired" in msg(), f"expired email link explained ({msg()})")

        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    srv.terminate(); mock.terminate()
    srv.wait()
    tb = [l for l in open(LOG).read().splitlines() if "Traceback" in l or "Error" in l]
    if tb: print("server errors:", tb[:6])
print(len(fails), "failure(s)")
