"""The start screen is a chat: ask for a graph, or drop a spreadsheet and get a
free one; tokens at the bottom; only the simple tools until "More tools".
Claude is faked here (figforge.assistant start/step), so no money is spent;
the server, its token counting and the page are real."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, shutil, sys, tempfile, threading, time
sys.path.insert(0, ROOT_DIR)
from http.server import ThreadingHTTPServer
from _cleanup import rmtree
from playwright.sync_api import sync_playwright

ROOT = ROOT_DIR; os.chdir(ROOT)
for k in list(os.environ):
    if k.startswith(("SUPABASE_", "FIGFORGE_")):
        del os.environ[k]
from figforge import assistant, server

S = OUT_DIR
AI_NAME, FREE_NAME = "zz_chat line", "zz_chat_free"
USAGE = f"figures/.usage/{time.strftime('%Y-%m')}.json"
kept_usage = open(USAGE, "rb").read() if os.path.exists(USAGE) else None


def clean():
    for n in (AI_NAME, FREE_NAME, "zz_chat nothing"):
        if os.path.exists(f"figures/{n}"):
            rmtree(f"figures/{n}")
    if os.path.exists(USAGE):
        os.remove(USAGE)


clean()
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m, flush=True); c or fails.append(m)


# ------------------------------------------------------------- a pretend Claude
def fake_start(fig, files, skipped, idea):
    return {"version": 2, "idea": idea, "messages": [{"role": "user", "content": []}], "tables": {},
            "anthropic_files": [], "steps": 0, "cost_usd": 0.0, "views": 0, "created": False,
            "done": False, "reply": "", "log": [], "chat": [{"role": "user", "text": idea}],
            "files_seen": [f["name"] for f in files]}


def fake_edit(fig, request):
    return dict(fake_start(fig, [], [], request), created=True, mode="edit")


def fake_continue(fig, state, message):
    state.update(done=False, idea=message, steps=0)
    state["chat"].append({"role": "user", "text": message})
    return state


def fake_step(state, fig):
    state["steps"] += 1
    state["cost_usd"] += 0.01
    if "nothing" in state["idea"]:
        return assistant._finish(state, "I couldn't draw that."), None, []
    if state["steps"] == 1 and not state["created"]:
        shutil.copytree("figures/qcircle/data", f"figures/{fig.name}/data", dirs_exist_ok=True)
        state["created"] = True
        return state, json.load(open("figures/qcircle/spec.json", encoding="utf-8")), ["Drew the graph"]
    if state["steps"] == 1 and "question" not in state["idea"]:
        spec = fig.load_spec()
        spec["panels"][0]["title"] = {**(spec["panels"][0].get("title") or {}), "text": "Changed"}
        return state, spec, ["Changed the graph"]
    return assistant._finish(state, "All done."), None, []


assistant.configured = lambda: True
assistant.start, assistant.start_edit, assistant.step = fake_start, fake_edit, fake_step
assistant.cleanup = lambda state: None
assistant.continue_chat = fake_continue
limited = {"on": False}
real_tokens = server.Handler._tokens
server.Handler._tokens = lambda self, user=None: {**real_tokens(self, user), "unlimited": not limited["on"]}

server.Handler.log_message = lambda *a: None
httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{httpd.server_address[1]}"

csv_path = os.path.join(tempfile.mkdtemp(), "zz_chat_free.csv")
open(csv_path, "w").write("t,v\n0,1\n1,3\n2,2\n3,5\n")
ready = "document.getElementById('status').dataset.ready === '1'"
shown = lambda pg, sel: pg.locator(sel).first.is_visible()

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        ctx = br.new_context(viewport={"width": 1400, "height": 850})
        ctx.route("**/api/assistant/howto/**", lambda r: r.fulfill(status=503, json={"error": "not in this test"}))
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))

        # ------------------------------------------------------- the start screen
        pg.goto(B + "/"); pg.wait_for_function(ready)
        check(pg.evaluate("document.body.classList.contains('at-home')") and pg.evaluate("FIGURE") is None,
              "no figure picked: the page opens on the chat")
        check(shown(pg, "#chat-input") and "What do you want to graph?" in pg.inner_text("#chat-hello"), "…asking what to graph")
        check(not any(shown(pg, s) for s in ("#menu-file", "#tool-label", "#btn-undo", "#btn-save-pic", "#btn-more", "#inspector")),
              "…with no tools in the way")
        check("100 tokens" in pg.inner_text("#sb-tokens"), f"100 tokens at the bottom ({pg.inner_text('#sb-tokens')})")
        check("50 tokens" in pg.inner_text("#chat-price") and "free" in pg.inner_text("#chat-price"), "it says what a graph costs")
        check(pg.locator('#home-mine .mine[data-name="qcircle"]').count() == 1, "your graphs are listed")
        check(pg.is_disabled("#chat-send"), "nothing to send yet")
        pg.screenshot(path=S + "/chat_home.png")

        # ---------------------------------------------- a dropped spreadsheet: free
        pg.set_input_files("#ai-files", csv_path)
        pg.wait_for_selector("#chat-offer:not([hidden])")
        offer = pg.inner_text("#chat-offer")
        check("for free" in offer and "no tokens" in offer and "50 tokens" in offer,
              f"a spreadsheet on its own: offered for free, and says what Claude would cost")
        check("zz_chat_free.csv" in pg.inner_text("#chat-files"), "the file shows by the chat box")
        pg.screenshot(path=S + "/chat_offer.png")
        pg.click("#chat-files button")
        check(pg.is_hidden("#chat-offer") and pg.is_hidden("#chat-files"), "× takes it out again")
        pg.set_input_files("#ai-files", csv_path)
        pg.click("#offer-free")
        pg.wait_for_selector("#csv-pick:not([hidden])")
        check("no tokens" in pg.inner_text("#csv-modal"), "the column picker says it's free")
        pg.click("#csv-create")
        pg.wait_for_url("**project=zz_chat_free"); pg.wait_for_function(ready)
        check(pg.evaluate("FIGURE") == FREE_NAME and pg.locator("#canvas svg").count() == 1, "the free graph opens")
        check("100 tokens" in pg.inner_text("#sb-tokens"), "…and no tokens were used")

        # ------------------------------------------------- the simple figure screen
        check(shown(pg, "#btn-undo") and shown(pg, "#btn-save-pic") and shown(pg, "#chat-input"),
              "with a graph: Undo, Save picture and the chat stay")
        check(not any(shown(pg, s) for s in ("#menu-file", "#tool-label", "#zoom", "#sb-list", "#inspector")),
              "…the rest is hidden")
        box = pg.locator("#canvas svg").bounding_box()
        # Pick the first thing the list offers, as a click on the figure would.
        pg.evaluate("document.querySelector('#element-list [data-id], #element-list button, #element-list li')?.click()")
        pg.wait_for_function("document.body.classList.contains('picked')")
        check(shown(pg, "#inspector") and pg.locator("#insp-body").is_visible(), "picking something brings its settings")
        side, chat = pg.locator(".sidebar").bounding_box(), pg.locator("#chatside").bounding_box()
        check(side["x"] == chat["x"] and side["width"] == chat["width"], "…over the chat")
        check(pg.locator("#canvas svg").bounding_box() == box, "…and the graph doesn't move or change size")
        pg.screenshot(path=S + "/chat_picked.png")
        pg.keyboard.press("Escape")
        check(pg.is_hidden("#inspector") and shown(pg, "#chat-input"), "letting go brings the chat back")
        pg.click("#btn-more")
        check(all(shown(pg, s) for s in ("#menu-file", "#tool-label", "#zoom", "#inspector")), "More tools shows everything")
        pg.screenshot(path=S + "/chat_more.png")
        pg.reload(); pg.wait_for_function(ready)
        check(shown(pg, "#menu-file"), "…and is remembered")
        pg.click("#btn-more")
        check(not shown(pg, "#menu-file") and pg.inner_text("#btn-more") == "More tools", "Fewer tools hides it again")

        # ---------------------------------------------------- asking Claude for one
        pg.click("#btn-home"); pg.wait_for_function("document.body.classList.contains('at-home')")
        check("project" not in pg.url, "the logo goes back to the start")
        pg.fill("#chat-input", "zz_chat nothing please")
        pg.click("#chat-send")
        pg.wait_for_function("document.getElementById('chat-msg').textContent.includes('couldn')")
        check(pg.evaluate("document.body.classList.contains('at-home')") and pg.input_value("#chat-input") == "zz_chat nothing please"
              and pg.locator("#ai-chat .chat-msg").count() == 0, "no graph made: back at the start with your words kept")
        check("100 tokens" in pg.inner_text("#sb-tokens"), "…and no tokens used")
        rmtree("figures/zz_chat nothing")

        pg.fill("#chat-input", "zz_chat line of something")
        pg.press("#chat-input", "Enter")
        pg.wait_for_function("FIGURE !== null", timeout=30000)
        name = pg.evaluate("FIGURE")
        pg.wait_for_function("!aiRunning", timeout=30000)
        check(name.startswith("zz_chat"), f"Claude's graph opens on the same page ({name})")
        check(not pg.evaluate("document.body.classList.contains('at-home')") and pg.locator("#canvas svg").count() == 1
              and "project=" in pg.url, "…beside the chat")
        msgs = pg.locator("#ai-chat .chat-msg")
        check(msgs.count() == 2 and "All done." in msgs.nth(1).inner_text() and "50 tokens" in msgs.nth(1).inner_text(),
              "…the answer says it used 50 tokens")
        check("50 tokens" in pg.inner_text("#sb-tokens"), f"…and the count went down ({pg.inner_text('#sb-tokens')})")
        pg.screenshot(path=S + "/chat_figure.png")

        # ------------------------------------------------------------ talking to it
        pg.fill("#chat-input", "just a question")
        pg.click("#chat-send"); pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 4 && !aiRunning")
        check("50 tokens" in pg.inner_text("#sb-tokens"), "a change that changes nothing is free")
        pg.fill("#chat-input", "make it bigger")
        pg.click("#chat-send"); pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 6 && !aiRunning")
        check("40 tokens" in pg.inner_text("#sb-tokens") and "10 tokens" in pg.locator("#ai-chat .chat-msg").nth(5).inner_text(),
              "a real change uses 10 tokens")
        check(pg.evaluate("spec.panels[0].title.text") == "Changed", "…and shows on the graph")
        pg.evaluate("undo()")
        check(pg.evaluate("spec.panels[0].title.text") != "Changed", "Undo takes it back")
        pg.reload(); pg.wait_for_function(ready)
        pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 6")
        check("10 tokens" in pg.locator("#ai-chat .chat-msg").nth(5).inner_text(), "the talk and its tokens come back on reload")

        # ------------------------------------------------------------ out of tokens
        limited["on"] = True
        server.TOKENS_MONTHLY = 65   # 60 used so far: 5 left, less than a change
        pg.reload(); pg.wait_for_function(ready)
        pg.fill("#chat-input", "one more")
        check(pg.is_disabled("#chat-send") and "you have 5" in pg.inner_text("#chat-price"), "not enough tokens: says so, can't send")
        st = pg.evaluate("""async () => { const r = await fetch('/api/assistant/edit/' + encodeURIComponent(FIGURE),
              {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({request:'x'})});
              return [r.status, (await r.json()).error]; }""")
        check(st[0] == 402 and "always free" in st[1], f"…and the server refuses too ({st[0]})")
        pg.click("#btn-home"); pg.wait_for_function("document.body.classList.contains('at-home')")
        pg.set_input_files("#ai-files", csv_path)
        pg.wait_for_selector("#chat-offer:not([hidden])")
        check(shown(pg, "#offer-free"), "the free graph is still on offer with no tokens")

        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    httpd.shutdown()
    for d in [d for d in os.listdir("figures") if d.startswith("zz_chat")]:
        rmtree(f"figures/{d}")
    clean()
    if kept_usage is not None:
        open(USAGE, "wb").write(kept_usage)

print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
