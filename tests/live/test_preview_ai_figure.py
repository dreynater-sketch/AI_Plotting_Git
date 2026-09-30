"""On a deployed URL: one complete AI figure, end to end, as an admin.
A small non-CSV instrument-style file forces Claude to use its sandbox, so
this covers the upload, sandbox rounds inside Vercel's time limit, the table
download, storage, and the finished figure. Real Claude calls: roughly
$0.10-0.40 per run.

    python tests/live/test_preview_ai_figure.py <deployment url> <_vercel_share token>
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

# A header block, then one count per line -- like the RBS .asc exports.
counts = [int(400 * math.exp(-((c - 120) / 18) ** 2) + 30 * math.exp(-c / 90)) for c in range(256)]
SPECTRUM = "<<Spectrum Name>>\ntest\n<<DATA>>\n" + "\n".join(map(str, counts)) + "\n<<END>>\n"
NOTES = "calibration: E(keV) = 7.5 * channel + 20\n"
IDEA = ("Plot the spectrum in spectrum.asc: counts vs channel. Put energy in keV on the "
        "top axis using the calibration in notes.txt. Label the peak.")

U = {"email": "figforge-ai-figure-test@example.com", "password": "ai-figure-pass-1234"}
uid = admin("POST", "/users", {"email": U["email"], "password": U["password"], "email_confirm": True,
                               "app_metadata": {"figforge_admin": True, "provider": "email",
                                                "providers": ["email"]},
                               "user_metadata": {"display_name": "AI Figure Tester"}})["id"]
try:
    with sync_playwright() as pw:
        ctx = pw.request.new_context()
        ctx.get(f"{H}/?_vercel_share={SHARE}")
        post = lambda path, body=None, timeout=300000: ctx.post(
            H + path, headers={"Content-Type": "application/json"},
            data=json.dumps(body or {}), timeout=timeout)
        check(post("/api/auth/login", U).ok, "temp admin logs in")
        r = post("/api/assistant/start", {"name": "ai_live", "idea": IDEA, "skipped": [],
                                          "files": [{"name": "spectrum.asc", "text": SPECTRUM},
                                                    {"name": "notes.txt", "text": NOTES}]})
        check(r.ok, f"start: files handed to Claude ({r.status} {r.text()[:120] if not r.ok else ''})")
        t, events, d = time.time(), [], {}
        for _ in range(25):
            s = post("/api/assistant/step/ai_live")
            if s.status in (503, 504):
                print("   (retrying a slow step)"); continue
            if not s.ok:
                check(False, f"step failed: {s.status} {s.text()[:200]}"); break
            d = s.json(); events += d["events"]
            if d["done"]:
                break
        print(f"   {time.time() - t:.0f}s, {d.get('steps')} steps, ${d.get('cost_usd')}")
        print("   events:", " | ".join(events))
        print("   reply:", d.get("reply"))
        check(d.get("done") and d.get("created"), "the session finished with a figure")
        check(any(e.startswith("Made a table") for e in events), "the sandbox produced a table")
        fig = ctx.get(H + "/api/figure/ai_live")
        spec = fig.json().get("spec", {}) if fig.ok else {}
        p = (spec.get("panels") or [{}])[0]
        check(fig.ok and p.get("series"), "the figure opens in the editor")
        top = p.get("top_axis") or {}
        check(abs(top.get("scale", 0) - 7.5) < 1e-6 and abs(top.get("offset", 0) - 20) < 1e-6,
              f"energy axis from the calibration ({top.get('scale')}, {top.get('offset')})")
        check(any(t.get("text") for t in p.get("texts", [])), "a peak label was added")
        ctx.dispose()
finally:
    keys = store._walk(f"users/{uid}")
    if keys:
        store._call("DELETE", "/object/figforge", {"prefixes": keys})
    admin("DELETE", f"/users/{uid}")
    print(f"cleanup: temp user deleted, {len(keys)} files removed")
print(len(fails), "failure(s)")
