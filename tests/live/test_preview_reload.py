"""On a deployed URL: ask for a graph in the chat, reload part way through, and
it still gets made, with Claude paid once. Needs real hosting: what matters is
what Vercel does with a step when the page goes away. Real Claude calls:
roughly $0.10-0.40 per run.

    python tests/live/test_preview_reload.py <deployment url> <_vercel_share token>
"""
import json, math, os, sys, time, urllib.request
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

U = {"email": "figforge-reload-test@example.com", "password": "reload-test-pass-1234"}
uid = admin("POST", "/users", {"email": U["email"], "password": U["password"], "email_confirm": True,
                               "app_metadata": {"figforge_admin": True, "provider": "email", "providers": ["email"]},
                               "user_metadata": {"display_name": "Reload Tester"}})["id"]
try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge"); ctx = br.new_context(viewport={"width": 1400, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{H}/?_vercel_share={SHARE}")
        pg.wait_for_selector("#auth-screen:not([hidden])", timeout=60000)
        f = 'form[data-view="login"]'
        pg.fill(f + " [name=email]", U["email"]); pg.fill(f + " [name=password]", U["password"])
        pg.click(f + " button[type=submit]")
        pg.wait_for_function("document.getElementById('status').dataset.ready === '1'", timeout=90000)
        check(pg.inner_text("#sb-tokens").endswith("No limit"), "the admin has no limit")
        spent = lambda: json.loads(store.scoped(uid).read(".usage/tokens.json") or b'{"spent": 0}')["spent"]

        pg.fill("#chat-input", "A sine wave from 0 to 10, with the first peak labelled")
        pg.click("#chat-send")
        pg.wait_for_function("location.search.includes('project=')", timeout=120000)
        time.sleep(4)        # a step is running now
        t = time.time(); pg.reload()
        print("   reloaded in the middle of a step", flush=True)
        pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length >= 2 || FIGURE !== null", timeout=90000)
        check(pg.evaluate("document.body.classList.contains('at-home')") is False, "after the reload it's still at work, not back at the start")
        pg.wait_for_function("FIGURE !== null && !aiRunning", timeout=540000)
        print(f"   finished {time.time() - t:.0f}s after the reload", flush=True)
        pg.wait_for_selector("#canvas svg", timeout=60000)
        check(pg.evaluate("spec.panels.length") >= 1, "the graph got made")
        name = pg.evaluate("FIGURE")
        d = ctx.request.get(f"{H}/api/assistant/chat/{name}").json()
        check(not d["running"] and d["turns"][-1]["role"] == "assistant", "the talk finished with an answer")
        cost = d["turns"][-1].get("cost_usd", 0)
        used = spent()
        print(f"   ${cost}, {used:.1f} tokens used, answer: {d['turns'][-1]['text'][:80]}", flush=True)
        # A lost step would be paid for without being counted in the session,
        # so compare what was charged with what the session says it cost.
        check(abs(used - cost / 0.005) <= 2, f"tokens match the session's cost ({used:.1f} vs {cost / 0.005:.1f})")

        # A follow-up changes earlier messages (the first turn's files are gone):
        # Opus 5.5 would refuse that if the old thinking were sent as it was.
        n = pg.locator("#ai-chat .chat-msg").count()
        pg.fill("#chat-input", "Make the title bigger")
        pg.click("#chat-send")
        pg.wait_for_function(f"document.querySelectorAll('#ai-chat .chat-msg').length === {n + 2} && !aiRunning", timeout=240000)
        last = pg.locator("#ai-chat .chat-msg").last.inner_text()
        check(pg.inner_text("#chat-msg") == "" and "Used" in last, f"a follow-up message works ({last[:70]!r})")
        check(spent() > used, "...and counts its tokens")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    keys = store._walk(f"users/{uid}")
    if keys:
        store._call("DELETE", "/object/figforge", {"prefixes": keys})
    admin("DELETE", f"/users/{uid}")
    print(f"cleanup: temp user deleted, {len(keys)} files removed")
print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
