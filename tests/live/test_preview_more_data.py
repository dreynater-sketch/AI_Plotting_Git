"""On a deployed URL, with a temporary confirmed user and online storage:
lines from another file, the saved figure.py following figure edits, and
the AI talk route (no Claude calls -- nothing is spent).

    python tests/live/test_preview_more_data.py <deployment url> <_vercel_share token>
"""
import json, os, sys, tempfile, time, urllib.request
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
U = {"email": "figforge-more-test@example.com", "password": "more-test-pass-1234"}
P = "zz more"
second = os.path.join(tempfile.mkdtemp(), "run2.csv")
open(second, "w").write("t,signal\n" + "".join(f"{i},{200 + i}\n" for i in range(5, 15)))
uid = admin("POST", "/users", {"email": U["email"], "password": U["password"], "email_confirm": True,
                               "user_metadata": {"display_name": "More Tester"}})["id"]
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
        api = lambda path, body=None: (ctx.request.post(H + path, data=body) if body is not None
                                       else ctx.request.get(H + path))
        r = api("/api/project/from-csv", {"name": P, "csv": "t,signal\n" + "".join(f"{i},{i * i}\n" for i in range(10)),
                                         "filename": "run1.csv", "x": 0, "ys": [1]})
        check(r.ok, f"deployed: a figure from a first file ({r.status})")
        pg.goto(f"{H}/?project={P.replace(' ', '%20')}")
        pg.wait_for_function("document.getElementById('status').dataset.ready === '1'", timeout=90000)

        pg.click("#menu-insert"); pg.click("#menu-add-curves")
        pg.set_input_files("#csv-file", second)
        pg.wait_for_selector("#csv-pick:not([hidden])", timeout=30000)
        pg.click("#csv-create")
        pg.wait_for_function("document.getElementById('csv-modal').hidden", timeout=60000)
        labels = pg.evaluate("spec.panels[0].series.map(s => s.label)")
        check(labels == ["signal", "signal (run2)"], f"deployed: lines from another file join the plot ({labels})")

        d = api(f"/api/script/{P}").json()
        mine = d["generated"] + "\nprint('mine')\n"
        r = api(f"/api/script/{P}", {"code": mine, "base_rev": d["rev"]})
        check(r.ok, "deployed: my figure.py is saved online")
        pg.evaluate("() => { pushHistory(); spec.panels[0].xlabel.text = 'time online'; scheduleSave(); }")
        for _ in range(100):
            if pg.evaluate("!savePending && !inFlight && spec.rev > %d" % d["rev"]):
                break
            time.sleep(0.2)
        c = api(f"/api/script/{P}").json()["custom"]
        check("print('mine')" in c["code"] and "'time online'" in c["code"] and c.get("merged") == {"conflicts": []},
              "deployed: my saved figure.py takes in the figure's change and keeps my line")

        t = api(f"/api/assistant/chat/{P}").json()
        check(t == {"turns": [], "can_continue": False, "running": False}, f"deployed: no AI talk yet ({t})")
        check(not errs, f"no page errors {errs}")
        pg.screenshot(path=os.path.join(ROOT, "tests", "live", "_out", "preview_more_data.png"))
        br.close()
finally:
    keys = store._walk(f"users/{uid}")
    if keys:
        store._call("DELETE", "/object/figforge", {"prefixes": keys})
    admin("DELETE", f"/users/{uid}")
    print(f"cleanup: temp user deleted, {len(keys)} files removed")
print(len(fails), "failure(s)")
