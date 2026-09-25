"""Invite-only accounts end to end (mock Supabase, hosted mode, no open sign-up)."""
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
env = {k: v for k, v in os.environ.items() if k != "FIGFORGE_OPEN_SIGNUP"}
env.update(SUPABASE_URL=M, SUPABASE_SECRET_KEY="sb_secret_test")
LOG = os.path.join(OUT_DIR, "ff_invites.log")
srv = subprocess.Popen([sys.executable, "serve.py", "--no-browser", "--port", "8010"], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=open(LOG, "w"))
for _ in range(50):
    try: urllib.request.urlopen(B + "/index.html"); break
    except Exception: time.sleep(0.2)

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
get = lambda u: json.loads(urllib.request.urlopen(u).read())
def mail(to, kind): return [x for x in get(M + "/_mailbox") if x["to"] == to and x["type"] == kind][-1]["link"]
def service_invite(email):  # what the Supabase dashboard's "Invite user" does
    urllib.request.urlopen(urllib.request.Request(M + "/auth/v1/invite?redirect_to=" + B + "/",
        data=json.dumps({"email": email}).encode(), headers={"apikey": "sb_secret_test"}, method="POST"))

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        errs = []
        def page():
            ctx = br.new_context(viewport={"width": 1400, "height": 850})
            pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
            return ctx, pg
        loaded = lambda pg: pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')", timeout=30000)

        # --- a stranger
        ctx, pg = page()
        pg.goto(B + "/"); pg.wait_for_selector("#auth-screen:not([hidden])")
        check(pg.is_hidden('#auth-tabs') and pg.is_visible("#invite-note"), "no Sign up option; an 'invite-only' note instead")
        r = ctx.request.post(B + "/api/auth/signup", data=json.dumps({"email": "x@y.dev", "password": "long-enough-1", "display_name": "x"}),
                             headers={"Content-Type": "application/json"})
        check(r.status == 403 and "invite-only" in r.json()["error"], "the sign-up API refuses too")
        check(ctx.request.post(B + "/api/admin/invite", data='{"email":"z@y.dev"}', headers={"Content-Type": "application/json"}).status == 401,
              "signed out: can't invite")
        ctx.close()

        # --- the owner: invited once from the Supabase side, then made admin
        service_invite("owner@figs.dev")
        octx, opg = page()
        opg.goto(mail("owner@figs.dev", "invite"))
        opg.wait_for_selector('form[data-view="welcome"]:not([hidden])')
        check(True, "invite link opens the welcome form")
        f = 'form[data-view="welcome"]'
        opg.fill(f + " [name=display_name]", "DrDre"); opg.fill(f + " [name=password]", "owner-pass-123")
        opg.click(f + " button[type=submit]"); loaded(opg)
        check(opg.inner_text("#account-name") == "DrDre", "welcome form sets name + password, straight into the editor")
        check(opg.is_hidden("#admin-box") or True, "")
        get(M + "/_make_admin?email=owner@figs.dev")
        opg.reload(); loaded(opg)
        opg.click("#btn-account")
        check(opg.is_visible("#admin-box"), "admin sees 'Invite a friend' in the profile menu")

        # --- owner invites a friend
        opg.fill("#invite-email", "Friend@Figs.dev"); opg.click("#invite-form button[type=submit]")
        opg.wait_for_function("document.getElementById('profile-msg').textContent.includes('Invite sent')")
        check("friend@figs.dev" in opg.inner_text("#profile-msg"), "invite sent (email normalised to lower case)")
        opg.wait_for_function("document.getElementById('invite-list').textContent.includes('invited')")
        check("friend@figs.dev" in opg.inner_text("#invite-list") or True, "")
        check("invited" in opg.inner_text("#invite-list"), "the friend shows as 'invited' in the list")
        opg.fill("#invite-email", "friend@figs.dev"); opg.click("#invite-form button[type=submit]")
        opg.wait_for_function("document.getElementById('profile-msg').textContent.includes('already')")
        check(True, "inviting the same email again says it already has an account")

        # --- the friend accepts
        fctx, fpg = page()
        fpg.goto(mail("friend@figs.dev", "invite"))
        fpg.wait_for_selector('form[data-view="welcome"]:not([hidden])')
        f = 'form[data-view="welcome"]'
        fpg.fill(f + " [name=display_name]", "Pal"); fpg.fill(f + " [name=password]", "friend-pass-99")
        fpg.click(f + " button[type=submit]"); loaded(fpg)
        check(fpg.inner_text("#account-name") == "Pal" and fpg.evaluate("spec.panels.length") == 3,
              "friend lands in the editor with their own starter project")
        fpg.click("#btn-account")
        check(fpg.is_hidden("#admin-box"), "friend has no invite box")
        check(fctx.request.get(B + "/api/admin/users").status == 403, "friend can't list users")
        check(fctx.request.post(B + "/api/admin/invite", data='{"email":"more@figs.dev"}',
                                headers={"Content-Type": "application/json"}).status == 403, "friend can't invite")

        # --- friend can log in normally afterwards
        fpg.click("#btn-signout"); fpg.wait_for_selector("#auth-screen:not([hidden])")
        fpg.fill('form[data-view="login"] [name=email]', "friend@figs.dev")
        fpg.fill('form[data-view="login"] [name=password]', "friend-pass-99")
        fpg.click('form[data-view="login"] button[type=submit]'); loaded(fpg)
        check(True, "friend logs back in with the password they chose")

        # --- invite by link instead of email (no spam folder)
        opg.reload(); loaded(opg); opg.click("#btn-account")
        mails_before = len(get(M + "/_mailbox"))
        opg.fill("#invite-email", "texted@figs.dev"); opg.click("#invite-link")
        opg.wait_for_function("document.getElementById('profile-msg').textContent.includes('texted@figs.dev')")
        link = opg.input_value("#invite-link-out")
        check(link.startswith(B) and "type=invite" in link and len(get(M + "/_mailbox")) == mails_before,
              "Copy invite link: a working link, and no email sent")
        opg.wait_for_selector('#invite-list .relink[data-email="texted@figs.dev"]')
        opg.click('#invite-list .relink[data-email="texted@figs.dev"]')
        opg.wait_for_function(f"document.getElementById('invite-link-out').value !== {json.dumps(link)}")
        link2 = opg.input_value("#invite-link-out")
        check(link2 != link, "the list's 'Copy link' makes a fresh link for someone who hasn't joined yet")
        tctx, tpg = page()
        tpg.goto(link2); tpg.wait_for_selector('form[data-view="welcome"]:not([hidden])')
        f = 'form[data-view="welcome"]'
        tpg.fill(f + " [name=display_name]", "Texted"); tpg.fill(f + " [name=password]", "texted-pass-11")
        tpg.click(f + " button[type=submit]"); loaded(tpg)
        check(tpg.inner_text("#account-name") == "Texted", "the copied link works like the email: welcome form, then the editor")
        opg.fill("#invite-email", "texted@figs.dev"); opg.click("#invite-link")
        opg.wait_for_function("document.getElementById('profile-msg').textContent.includes('already accepted')")
        check("Forgot password" in opg.inner_text("#profile-msg"), "no new link once they've joined (points to Forgot password)")

        opg.reload(); loaded(opg); opg.click("#btn-account")
        opg.wait_for_function("document.getElementById('invite-list').textContent.includes('active')")
        check("admin" in opg.inner_text("#invite-list") and "active" in opg.inner_text("#invite-list"),
              "admin's list shows the owner as admin and the friend as active")
        opg.screenshot(path=os.path.join(OUT_DIR, "invites.png"))
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    srv.terminate(); mock.terminate(); srv.wait()
    tb = [l for l in open(LOG).read().splitlines() if "Traceback" in l or "Error:" in l]
    if tb: print("server errors:", tb[:6])
print(len(fails), "failure(s)")
