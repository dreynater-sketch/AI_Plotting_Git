"""On a deployed URL: the Claude connection check (/api/admin/ai-ping) works
with the deployment's ANTHROPIC_API_KEY, and only admins can call it.
Makes one tiny, real Claude request (well under a cent).

    python tests/live/test_preview_ai.py <deployment url> <_vercel_share token>
"""
import json, os, sys, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
H, SHARE = sys.argv[1], sys.argv[2]
env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
URL, KEY = env["SUPABASE_URL"], env["SUPABASE_SECRET_KEY"]


def admin(method, path, body=None):
    req = urllib.request.Request(URL + "/auth/v1/admin" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"apikey": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(); return json.loads(raw) if raw else {}


fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)
U = {"email": "figforge-ai-test@example.com", "password": "ai-test-pass-1234"}
# A $0 personal AI cap: the member is offered AI but refused for budget before
# anything reaches Claude.
uid = admin("POST", "/users", {"email": U["email"], "password": U["password"], "email_confirm": True,
                               "app_metadata": {"figforge_ai_cap": 0, "provider": "email", "providers": ["email"]},
                               "user_metadata": {"display_name": "AI Tester"}})["id"]
try:
    with sync_playwright() as pw:
        ctx = pw.request.new_context()
        ctx.get(f"{H}/?_vercel_share={SHARE}")          # sets the Vercel access cookie
        login = lambda: ctx.post(H + "/api/auth/login", headers={"Content-Type": "application/json"},
                                 data=json.dumps(U))
        check(ctx.get(H + "/api/admin/ai-ping").status == 401, "signed out: ai-ping refused")
        check(login().ok, "temp user logs in")
        check(ctx.get(H + "/api/admin/ai-ping").status == 403, "regular member: ai-ping refused")
        me = ctx.get(H + "/api/auth/me").json()
        check(me.get("assistant") is True and me.get("ai_usage", {}).get("cap") == 0,
              f"member: offered AI with their own cap ({me.get('ai_usage')})")
        r = ctx.post(H + "/api/assistant/start", headers={"Content-Type": "application/json"},
                     data=json.dumps({"name": "zz_ai", "files": [{"name": "a.csv", "text": "x,y\n1,2\n"}],
                                      "idea": "plot it"}))
        check(r.status == 402, f"member over the cap: AI refused before reaching Claude ({r.status})")
        check(ctx.post(H + "/api/assistant/step/zz_ai").status == 402, "member over the cap: no steps either")

        admin("PUT", f"/users/{uid}", {"app_metadata": {"figforge_admin": True,
                                                        "provider": "email", "providers": ["email"]}})
        login()   # the server caches users per token for 5 min -- a fresh login sees the admin flag
        check(ctx.get(H + "/api/auth/me").json().get("assistant") is True, "admin: AI box offered")
        r = ctx.get(H + "/api/admin/ai-ping", timeout=90000)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check(r.ok and body.get("ok"), f"admin: Claude answered ({r.status})")
        if body.get("reply"):
            print(f"   {body.get('model')}: {body['reply']}")
            print(f"   {body.get('input_tokens')} in + {body.get('output_tokens')} out = ${body.get('cost_usd')}")
        elif body:
            print("  ", body)
        ctx.dispose()
finally:
    admin("DELETE", f"/users/{uid}")
    print("cleanup: temp user deleted")
print(len(fails), "failure(s)")
