"""On a deployed URL: invite-only sign-up, CSV import, and the assistant
endpoints, with a temporary confirmed user (admin-created, no email sent).

    python tests/live/test_preview_friends.py <deployment url> <_vercel_share token>
"""
import json, os, sys, tempfile, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
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
U = {"email": "figforge-friends-test@example.com", "password": "friends-test-pass-1234"}
uid = admin("POST", "/users", {"email": U["email"], "password": U["password"], "email_confirm": True,
                               "user_metadata": {"display_name": "Friend Tester"}})["id"]
csv_path = os.path.join(tempfile.mkdtemp(), "decay.csv")
open(csv_path, "w").write("t,counts\n" + "\n".join(f"{i},{1000 * 0.9 ** i:.2f}" for i in range(30)) + "\n")
try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge"); ctx = br.new_context(viewport={"width": 1400, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{H}/?_vercel_share={SHARE}")
        pg.wait_for_selector("#auth-screen:not([hidden])", timeout=60000)
        check(pg.is_hidden("#auth-tabs") and pg.is_visible("#invite-note"), "deployed: invite-only log-in screen")
        r = ctx.request.post(H + "/api/auth/signup", data=json.dumps({"email": "x@example.com", "password": "long-enough-1",
                                                                      "display_name": "x"}), headers={"Content-Type": "application/json"})
        check(r.status == 403, f"deployed: public sign-up refused ({r.status})")
        f = 'form[data-view="login"]'
        pg.fill(f + " [name=email]", U["email"]); pg.fill(f + " [name=password]", U["password"])
        pg.click(f + " button[type=submit]")
        pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')", timeout=90000)
        pg.click("#btn-account")
        check(pg.is_hidden("#admin-box"), "deployed: a regular member has no invite box")
        check(ctx.request.get(H + "/api/admin/users").status == 403, "deployed: members can't list users")
        pg.keyboard.press("Escape")
        pg.mouse.click(5, 300)

        pg.click("#btn-new"); pg.set_input_files("#csv-file", csv_path)
        pg.wait_for_selector("#csv-pick:not([hidden])", timeout=30000)
        pg.click("#csv-create")
        pg.wait_for_url("**/?project=decay", timeout=60000)
        pg.wait_for_function("document.getElementById('status').textContent.startsWith('rev')", timeout=60000)
        check(pg.evaluate("spec.panels[0].series[0].label") == "counts", "deployed: New from CSV builds a figure")
        check(any(k.endswith("decay/data/source.csv") for k in store._walk(f"users/{uid}")), "deployed: source CSV stored in the user's space")

        # invite by link against real Supabase: the temp user becomes admin
        # for a moment; the invited example.com address never gets an email
        admin("PUT", f"/users/{uid}", {"app_metadata": {"figforge_admin": True, "provider": "email", "providers": ["email"]}})
        # a fresh session: the server caches who a token belongs to for a few minutes
        ctx.request.post(H + "/api/auth/login", data=json.dumps({"email": U["email"], "password": U["password"]}),
                         headers={"Content-Type": "application/json"})
        r = ctx.request.post(H + "/api/admin/invite-link", data=json.dumps({"email": "figforge-linkcheck@example.com"}),
                             headers={"Content-Type": "application/json"})
        link = r.json().get("link", "") if r.ok else ""
        check(r.ok and ".supabase.co/auth/v1/verify" in link and "type=invite" in link,
              f"deployed: Copy invite link returns a real Supabase invite link ({r.status})")
        for u in admin("GET", "/users?per_page=200")["users"]:
            if u.get("email") == "figforge-linkcheck@example.com":
                admin("DELETE", f"/users/{u['id']}")

        tools = ctx.request.get(H + "/api/assistant/tools").json()
        check(tools["model"] == "claude-opus-5" and len(tools["tools"]) == 10, "deployed: assistant tool definitions served")
        r = ctx.request.post(H + "/api/ops/decay", data=json.dumps({"calls": [
            {"name": "set_axis", "input": {"panel_id": "a", "axis": "y", "min": 1, "max": 2000, "scale": "log", "tick_size": None}},
            {"name": "set_text", "input": {"element_id": "a__title", "text": "Decay (log scale)"}}]}),
            headers={"Content-Type": "application/json"}).json()
        check(r["changed"] and not any(x["is_error"] for x in r["results"]) and "<svg" in r.get("svg", ""),
              "deployed: /api/ops applies tool calls and returns the new render")
        pg.screenshot(path=os.path.join(ROOT, "tests", "_out", "preview_friends.png"))
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    keys = store._walk(f"users/{uid}")
    if keys:
        store._call("DELETE", "/object/figforge", {"prefixes": keys})
    admin("DELETE", f"/users/{uid}")
    print(f"cleanup: temp user deleted, {len(keys)} files removed")
print(len(fails), "failure(s)")
