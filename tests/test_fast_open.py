"""Opening a saved figure quickly: the page asks for the figure, its undo
history and the project list all at once (not one after another); the
server hands back a figure it drew a moment ago from memory; the page's
own files are only re-sent when they change."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import json, os, sys, time, urllib.error, urllib.request
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
from playwright.sync_api import sync_playwright

B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765")
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)

# --- the page's files: sent once, then only "still the same"
r = urllib.request.urlopen(B + "/app.js"); etag = r.headers["ETag"]
check(bool(etag) and r.headers["Cache-Control"] == "no-cache", "app.js comes with a fingerprint")
try:
    urllib.request.urlopen(urllib.request.Request(B + "/app.js", headers={"If-None-Match": etag}))
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
check(code == 304, f"asked again with that fingerprint: nothing re-sent ({code})")
check(urllib.request.urlopen(B + "/api/figures").headers["Cache-Control"] == "no-store",
      "figure data is never cached")

# --- the second open of the same figure is not drawn again
def timed(url):
    t = time.time(); body = urllib.request.urlopen(url).read(); return time.time() - t, json.loads(body)
t1, a = timed(B + "/api/figure/qcircle")
t2, b = timed(B + "/api/figure/qcircle")
check(a["svg"] == b["svg"] and a["geometry"] == b["geometry"], "the remembered drawing is the same picture")
check(t2 < t1 / 2 or t2 < 0.1, f"…and comes back faster ({t1:.2f}s -> {t2:.2f}s)")

# --- the page asks for everything at once
with sync_playwright() as pw:
    br = pw.chromium.launch(channel="msedge")
    pg = br.new_page(viewport={"width": 1400, "height": 850}); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    sent, done, all_sent = {}, {}, []
    key = lambda u: u.split("?")[0].replace(B, "")
    pg.on("request", lambda q: (sent.setdefault(key(q.url), time.time()), all_sent.append(key(q.url))))
    pg.on("requestfinished", lambda q: done.setdefault(key(q.url), time.time()))
    pg.goto(f"{B}/?project=qcircle")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    me, figs, fig = "/api/auth/me", "/api/figures", "/api/figure/qcircle"
    check(all(k in sent for k in (me, figs, fig, "/api/history/qcircle")), "asked for who, projects, figure and history")
    check(sent[fig] < done[me] and sent[fig] < done[figs],
          "the figure is asked for before the other answers come back")
    check(all_sent.count(fig) == 1, f"…and only once ({all_sent.count(fig)})")
    # a figure name that doesn't exist: the early guess is dropped, the first project opens
    pg.goto(f"{B}/?project=zz_not_there")
    pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
    check(pg.evaluate("FIGURE") != "zz_not_there" and pg.evaluate("spec") is not None,
          "a project that isn't there: the first one opens instead")
    check(not errs, f"no page errors {errs}")
    br.close()

print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
