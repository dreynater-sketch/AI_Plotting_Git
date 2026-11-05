"""The start screen is a chat: ask for a graph, or drop a spreadsheet and get a
free one; tokens taken at what Claude costs; a reload carries on; only the simple tools until "More tools".
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
USAGE = "figures/.usage/tokens.json"
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


steps_run = []


def fake_step(state, fig):
    if state["done"]:             # as the real one: a finished session calls nothing
        return state, None, []
    steps_run.append(fig.name)
    if "slow" in state["idea"]:
        time.sleep(2.5)
    state["steps"] += 1
    state["cost_usd"] += 0.05     # 10 tokens
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
real_default = server.TOKENS_TRIAL
limited = {"on": True}       # a member, not the admin
server.TOKENS_TRIAL = 100
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
        check("30 tokens" in pg.inner_text("#chat-price") and "free" in pg.inner_text("#chat-price"), "it says what a graph costs")
        check(pg.locator('#home-mine .mine[data-name="qcircle"]').count() == 1, "your graphs are listed")
        check(pg.is_disabled("#chat-send"), "nothing to send yet")
        rail = pg.locator("#rail").bounding_box()
        check(rail["x"] == 0 and rail["height"] == 850 and rail["width"] == 280 and all(shown(pg, s) for s in ("#rail-new", "#rail-search", "#rail-toggle")),
              "the bar on the left runs the full height, with New graph and Search")
        size = lambda sel: pg.evaluate(f"getComputedStyle(document.querySelector({json.dumps(sel)})).fontSize")
        weight = lambda sel: pg.evaluate(f"getComputedStyle(document.querySelector({json.dumps(sel)})).fontWeight")
        check(size("#rail-new span") == size("#home-mine .mine") == "15px" and weight("#home-mine .mine") == "500",
              "its rows are all one size, plain not bold")
        pg.click("#rail-search"); pg.fill("#rail-find", "qcir")
        vis = pg.evaluate("[...document.querySelectorAll('#home-mine .mine')].filter((b) => !b.hidden).map((b) => b.dataset.name)")
        check(vis == ["qcircle"], f"Search finds a graph by name ({vis})")
        pg.click("#rail-search")
        check(pg.is_hidden("#rail-find") and pg.locator("#home-mine .mine:not([hidden])").count() > 1, "…and closing it brings them all back")
        pg.click("#rail-toggle")
        pg.wait_for_timeout(100)
        check(pg.locator("#rail").bounding_box()["width"] == 60 and not shown(pg, "#home-mine") and shown(pg, "#rail-new"),
              "it folds to just pictures")
        pg.click("#rail-toggle")
        pg.screenshot(path=S + "/chat_home.png")

        # ---------------------------------------------- a dropped spreadsheet: free
        pg.set_input_files("#ai-files", csv_path)
        pg.wait_for_selector("#chat-offer:not([hidden])")
        offer = pg.inner_text("#chat-offer")
        check("for free" in offer and "no tokens" in offer and "30 tokens" in offer,
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
        check(pg.locator("#rail").bounding_box()["width"] == 60, "beside a graph the bar is folded, to leave it room")
        pg.click("#rail-toggle"); pg.wait_for_timeout(300)
        check(pg.locator('#home-mine .mine.on[data-name="zz_chat_free"]').count() == 1, "…open, it shows which graph this is")
        pg.screenshot(path=S + "/chat_rail_open.png")
        pg.click("#rail-toggle"); pg.wait_for_timeout(300)
        check(shown(pg, "#btn-undo") and shown(pg, "#btn-save-pic") and shown(pg, "#chat-input"),
              "with a graph: Undo, Save picture and the chat stay")
        check(not any(shown(pg, s) for s in ("#menu-file", "#tool-label", "#zoom", "#sb-list", "#inspector")),
              "…the rest is hidden")
        chat0 = pg.locator("#chatside").bounding_box()
        box = pg.locator("#canvas svg").bounding_box()
        # A real press on the line, at its point (1, 3), and a drag.
        at = pg.evaluate("""(() => { const g = Object.values(geometry.panels)[0], r = svgEl.getBoundingClientRect(), k = r.width / geometry.width;
              return [r.left + (g.bbox[0] + (1 - g.xlim[0]) / (g.xlim[1] - g.xlim[0]) * g.bbox[2]) * k,
                      r.top + (g.bbox[1] + (1 - (3 - g.ylim[0]) / (g.ylim[1] - g.ylim[0])) * g.bbox[3]) * k]; })()""")
        pg.mouse.move(*at); pg.mouse.down(); pg.mouse.move(at[0] + 15, at[1] + 10, steps=3)
        pg.wait_for_timeout(200)
        check(pg.locator("#canvas svg").bounding_box() == box and pg.is_hidden("#inspector"),
              "with the button held down, the graph doesn't move or change size")
        pg.mouse.up()
        pg.wait_for_selector("#inspector", state="visible")
        check(pg.evaluate("selected().length") == 1 and pg.locator("#insp-body").is_visible(), "letting go shows its settings")
        pg.wait_for_timeout(300)
        side, chat = pg.locator(".sidebar").bounding_box(), pg.locator("#chatside").bounding_box()
        wrap = pg.locator("#canvas-wrap").bounding_box()
        check(chat == chat0 and shown(pg, "#chat-input") and shown(pg, "#chat-send"), "the chat stays where it was, not covered")
        check(side["x"] >= wrap["x"] + wrap["width"] - 1 and side["x"] >= chat["x"] + chat["width"],
              "…the settings are on the right, beside the graph")
        box2 = pg.locator("#canvas svg").bounding_box()
        check(box2["x"] + box2["width"] <= side["x"] + 1, "…and the whole graph still shows")
        pg.screenshot(path=S + "/chat_picked.png")
        pg.fill("#chat-input", "you can type here while it's open")
        pg.fill("#chat-input", "")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        check(pg.evaluate("selected().length") == 0 and shown(pg, "#inspector") and pg.locator("#canvas svg").bounding_box() == box2,
              "letting go of it keeps the panel, so the graph doesn't jump")
        check(not pg.locator("#insp-empty .subhead").first.is_visible(), "…with just a hint in it")
        pg.evaluate("document.querySelector('#element-list [data-id], #element-list button, #element-list li')?.click()")
        pg.wait_for_timeout(300)
        check(pg.locator("#canvas svg").bounding_box() == box2, "picking again doesn't move the graph either")
        pg.click("#btn-side-close")
        pg.wait_for_timeout(300)
        check(pg.is_hidden("#inspector") and pg.evaluate("selected().length") == 0
              and pg.locator("#canvas svg").bounding_box()["width"] > box2["width"], "× closes it and the graph gets its room back")
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
        check(pg.locator("#rail").bounding_box()["width"] == 280, "…where the bar is open again")
        pg.fill("#chat-input", "zz_chat nothing please")
        pg.click("#chat-send")
        pg.wait_for_function("document.getElementById('chat-msg').textContent.includes('couldn')")
        check(pg.evaluate("document.body.classList.contains('at-home')") and pg.input_value("#chat-input") == "zz_chat nothing please"
              and pg.locator("#ai-chat .chat-msg").count() == 0, "no graph made: back at the start with your words kept")
        check("90 tokens" in pg.inner_text("#sb-tokens"), f"…and it took only what Claude cost ({pg.inner_text('#sb-tokens')})")
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
        check(msgs.count() == 2 and "All done." in msgs.nth(1).inner_text() and "20 tokens" in msgs.nth(1).inner_text(),
              "…the answer says what it really used")
        check("70 tokens" in pg.inner_text("#sb-tokens"), f"…and the count went down by that ({pg.inner_text('#sb-tokens')})")
        pg.screenshot(path=S + "/chat_figure.png")

        # ------------------------------------------------------------ talking to it
        pg.fill("#chat-input", "just a question")
        pg.click("#chat-send"); pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 4 && !aiRunning")
        check("60 tokens" in pg.inner_text("#sb-tokens"), "a question costs what Claude cost too")
        pg.fill("#chat-input", "make it bigger")
        pg.click("#chat-send"); pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 6 && !aiRunning")
        check("40 tokens" in pg.inner_text("#sb-tokens") and "20 tokens" in pg.locator("#ai-chat .chat-msg").nth(5).inner_text(),
              "a change takes what it cost")
        check(pg.evaluate("spec.panels[0].title.text") == "Changed", "…and shows on the graph")
        pg.evaluate("undo()")
        check(pg.evaluate("spec.panels[0].title.text") != "Changed", "Undo takes it back")
        pg.reload(); pg.wait_for_function(ready)
        pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 6")
        check("20 tokens" in pg.locator("#ai-chat .chat-msg").nth(5).inner_text(), "the talk and its tokens come back on reload")

        # --------------------------------------------- a reload carries on with it
        pg.click("#btn-home"); pg.wait_for_function("document.body.classList.contains('at-home')")
        steps_run.clear()
        pg.fill("#chat-input", "zz_chat slow one")
        pg.click("#chat-send")
        pg.wait_for_function("location.search.includes('project=')")
        check(pg.evaluate("FIGURE") is None and pg.evaluate("localStorage.getItem('figforge.making')") == "zz_chat slow one",
              "while it's being made, the page remembers it")
        time.sleep(0.4)
        pg.reload()
        pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg').length === 2", timeout=20000)
        check(pg.locator("#ai-chat .chat-msg.you").inner_text() == "zz_chat slow one" and pg.evaluate("aiRunning"),
              "reloading before the graph is drawn: the talk is back and it keeps working")
        pg.wait_for_function("FIGURE !== null", timeout=30000)
        pg.wait_for_selector("#canvas svg", timeout=20000)
        check(pg.locator("#canvas svg").count() == 1, "…and the graph appears")
        time.sleep(0.4)
        pg.reload(); pg.wait_for_function(ready)
        pg.wait_for_function("document.querySelector('#ai-chat .chat-msg.ai') && !aiRunning", timeout=30000)
        check("All done." in pg.locator("#ai-chat .chat-msg").last.inner_text(), "reloading after it's drawn: it finishes too")
        check(len(steps_run) == 2, f"Claude ran each step once, reloads or not ({len(steps_run)})")
        check("20 tokens" in pg.inner_text("#sb-tokens"), f"…and was paid for once ({pg.inner_text('#sb-tokens')})")
        check(pg.evaluate("localStorage.getItem('figforge.making')") is None, "…and is forgotten once done")
        pg.goto(B + "/"); pg.wait_for_function(ready)
        check(pg.evaluate("document.body.classList.contains('at-home')"), "a finished graph doesn't reopen by itself")

        # --------------------------------------------- nothing is topped back up
        check(json.load(open(USAGE))["spent"] == 80, "the trial isn't by the month: one count for good")

        # ------------------------------------------------------------ out of tokens
        pg.goto(B + "/?project=" + name.replace(" ", "%20")); pg.wait_for_function(ready)
        server.TOKENS_TRIAL = 85     # 80 used: 5 left, just enough to start
        pg.reload(); pg.wait_for_function(ready); pg.wait_for_function("!aiRunning")
        pg.fill("#chat-input", "make it bigger")
        pg.click("#chat-send"); pg.wait_for_function("document.querySelectorAll('#ai-chat .chat-msg.you').length === 4 && !aiRunning")
        last = pg.locator("#ai-chat .chat-msg").last.inner_text()
        check("ran out of tokens" in last and pg.evaluate("spec.panels[0].title.text") == "Changed",
              f"running out part way: stops and keeps what's done ({last!r})")
        check("0 tokens" in pg.inner_text("#sb-tokens"), f"…at 0 ({pg.inner_text('#sb-tokens')})")
        check(pg.is_disabled("#chat-input") and pg.is_disabled("#chat-send") and "you have 0" in pg.inner_text("#chat-price"),
              "with none left: says so, can't type or send")
        st = pg.evaluate("""async () => { const r = await fetch('/api/assistant/edit/' + encodeURIComponent(FIGURE),
              {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({request:'x'})});
              return [r.status, (await r.json()).error]; }""")
        check(st[0] == 402 and "always free" in st[1], f"…and the server refuses too ({st[0]})")
        pg.click("#btn-home"); pg.wait_for_function("document.body.classList.contains('at-home')")
        pg.set_input_files("#ai-files", csv_path)
        pg.wait_for_selector("#chat-offer:not([hidden])")
        check(shown(pg, "#offer-free"), "the free graph is still on offer with no tokens")
        check("tokens" not in pg.inner_text("#chat-offer-or") and "spreadsheet" in pg.inner_text("#chat-hello")
              and not shown(pg, "#home-ideas") and pg.is_disabled("#chat-input"),
              "the start screen only offers what works with no tokens")
        pg.screenshot(path=S + "/chat_no_tokens.png")

        # ------------------------------------------- nobody gets any free by default
        check(real_default == 0, f"a new account starts with none ({real_default})")

        # ----------------------------------------------- the admin is never stopped
        limited["on"] = False
        pg.reload(); pg.wait_for_function(ready)
        check(pg.inner_text("#sb-tokens").endswith("No limit") and not pg.is_disabled("#chat-input"),
              "no limit: says so, and the chat works at any count")

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
