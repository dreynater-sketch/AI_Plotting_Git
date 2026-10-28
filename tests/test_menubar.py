"""The menu bar (Figure, Edit, Add, Look, Code, Ask Claude, Help): menus open and
close by click, pointing and keys; Add adds a legend, grid lines, a second
scale on the right and a shaded range; View shows or hides the list of
everything and the "Change it" panel (remembered); zoom from the bottom bar;
Start over asks first. The left tool strip (one-click tools, names shown by
default, folded away and back) and the bottom bar (what's picked, the pointer's x/y in the plot's
numbers, the figure size, the list)."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import json, os, sys, urllib.request
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)
from playwright.sync_api import sync_playwright

B = _os.environ.get("FF_BASE", "http://127.0.0.1:8765"); P = "zz_menubar"
rmtree(f"figures/{P}")
urllib.request.urlopen(urllib.request.Request(B + "/api/project/duplicate",
    data=json.dumps({"from": "qcircle", "name": P}).encode(), method="POST",
    headers={"Content-Type": "application/json"}))
if os.path.exists(f"figures/{P}/history.json"):
    os.remove(f"figures/{P}/history.json")  # start with nothing to undo
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
disk = lambda: json.load(open(f"figures/{P}/spec.json", encoding="utf-8"))
panel = lambda pid: next(p for p in disk()["panels"] if p["id"] == pid)

try:
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge")
        ctx = br.new_context(viewport={"width": 1400, "height": 850})
        pg = ctx.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"{B}/?project={P}")
        pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
        settle = lambda: pg.wait_for_function("!savePending && !inFlight && !needsSave", timeout=30000)
        ev = pg.evaluate
        shown = lambda sel: ev(f"!!document.querySelector('{sel}')?.checkVisibility()")

        # --- a clean screen: no list, no menus, no extra button rows
        check(not shown("#dock") and not shown(".menu"), "starts clean: no list under the figure, no menu open")
        loose = ev("[...document.querySelectorAll('header button:not(.menu-title)')]"
                   ".filter((b) => b.checkVisibility()).map((b) => b.id)")
        check(set(loose) <= {"btn-undo", "btn-redo", "btn-account"},
              f"only the menu names, undo/redo and the profile show in the top bar ({loose})")
        check(ev("document.querySelector('.canvas-area').clientHeight") > 700, "the figure gets almost the whole height")

        # --- open, switch by pointing, close
        pg.click("#menu-file")
        check(shown("#btn-new") and pg.get_attribute("#menu-file", "aria-expanded") == "true", "File opens its menu")
        pg.hover("#menu-insert")
        check(shown("#btn-add-label") and not shown("#btn-new"), "pointing at Insert switches to its menu")
        pg.mouse.click(700, 500)
        check(not shown(".menu"), "a click elsewhere closes it")
        pg.click("#menu-edit"); pg.keyboard.press("Escape")
        check(not shown(".menu"), "Esc closes it")

        # --- keys walk the menu and don't nudge the picked element
        ev("setSelection(['a_title'])"); before = ev("JSON.stringify(spec.panels[0].title)")
        pg.click("#menu-view"); pg.keyboard.press("ArrowDown")
        check(ev("document.activeElement.id") == "menu-zoom-in", "↓ goes to the first choice")
        pg.keyboard.press("ArrowRight")
        check(shown("#py-view"), "→ goes to the next menu (Code)")
        check(ev("JSON.stringify(spec.panels[0].title)") == before, "the arrow keys didn't move the picked title")
        pg.keyboard.press("Escape"); ev("setSelection([])")

        # --- Edit: undo/redo follow history
        check(ev("document.getElementById('menu-undo').disabled"), "Edit > Undo is greyed out with nothing to undo")

        # --- Insert
        pg.click("#menu-insert"); pg.click("#menu-add-grid"); settle()
        check(panel("a").get("grid") == "major" and not shown(".menu"), "Insert > Grid lines turns them on and closes the menu")
        check(not ev("document.getElementById('menu-undo').disabled"), "Edit > Undo is ready after a change")
        ev("setSelection(['b_meas'])")
        pg.click("#menu-insert"); pg.click("#menu-add-twin"); settle()
        check(next(c for c in panel("b")["series"] if c["id"] == "b_meas").get("axis") == "right"
              and "twin" in panel("b"), "Insert > Second scale puts the picked line on its own right-side scale")
        ev("setSelection(['panel:c'])")
        pg.click("#menu-insert"); pg.click("#menu-add-span"); settle()
        check(len(panel("c").get("spans") or []) == 1, "Insert > Shaded range adds one to the plot you're in")
        ev("setSelection(['panel:c'])")
        ev("document.getElementById('f-legend-on').checked = false;"
           "document.getElementById('f-legend-on').dispatchEvent(new Event('change'))"); settle()
        check(panel("c").get("legend") is None, "(legend hidden to start)")
        pg.click("#menu-insert"); pg.click("#menu-add-legend"); settle()
        check(panel("c").get("legend") and ev("selection[0]") == "c__legend",
              "Insert > Legend brings it back and picks it so its settings show")
        ev("document.getElementById('f-legend-on').checked = false;"
           "document.getElementById('f-legend-on').dispatchEvent(new Event('change'))"); settle()
        ev("setSelection([])"); pg.click("#tool-legend"); settle()
        with_lines = [p["id"] for p in disk()["panels"] if p.get("series")]
        check(len(with_lines) > 1 and all(panel(i).get("legend") for i in with_lines)
              and sorted(ev("selection")) == sorted(f"{i}__legend" for i in with_lines),
              f"nothing picked: Legend picks every graph's legend, showing hidden ones ({ev('selection')})")
        ev("setSelection([])")

        # --- View: numbers of a line without the list; the list; the side panel
        ev("setSelection(['a_meas'])")
        pg.wait_for_selector("#data-panel:not([hidden])")
        check(shown("#data-panel") and not shown("#element-list"), "picking a line shows its numbers, not the whole list")
        ev("setSelection([])")
        check(not shown("#dock"), "letting go hides them again")
        pg.click("#menu-view"); pg.click("#menu-view-list")
        check(shown("#element-list") and pg.get_attribute("#menu-view-list", "aria-checked") == "true",
              "View > List of everything shows it, with a tick")
        pg.click("#menu-view"); pg.click("#menu-view-side")
        check(not shown(".sidebar"), "View > \"Change it\" panel hides the right side")
        pg.reload(); pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
        check(shown("#element-list") and not shown(".sidebar"), "both choices are remembered after a reload")
        pg.click("#menu-view"); pg.click("#menu-view-side")
        pg.click("#menu-view"); pg.click("#menu-view-list")

        # --- the figure follows its space while the zoom is "fit"
        pg.set_viewport_size({"width": 1400, "height": 600}); pg.wait_for_timeout(400)  # height-bound
        ev("setSelection([])"); pg.wait_for_timeout(300); z0 = ev("zoom")
        ev("setSelection(['a_meas'])"); pg.wait_for_selector("#data-panel:not([hidden])"); pg.wait_for_timeout(400)
        z1 = ev("zoom")
        fits = ev("(() => { const w = document.getElementById('canvas-wrap'); const r = svgEl.getBoundingClientRect();"
                  " const b = w.getBoundingClientRect(); return r.bottom <= b.bottom + 1; })()")
        check(z1 < z0 and fits, f"picking a curve opens its numbers and the figure shrinks to stay whole ({z0}% -> {z1}%)")
        ev("setSelection([])"); pg.wait_for_timeout(400)
        check(ev("zoom") == z0, f"letting go gives the room back ({ev('zoom')}%)")
        pg.click("#zoom-in"); z2 = ev("zoom")
        ev("setSelection(['a_meas'])"); pg.wait_for_selector("#data-panel:not([hidden])"); pg.wait_for_timeout(400)
        check(ev("zoom") == z2, "a zoom set by hand stays put")
        ev("setSelection([])"); pg.click("#btn-fit")
        pg.set_viewport_size({"width": 1400, "height": 850}); pg.wait_for_timeout(300)

        # --- zoom from the corner
        z = ev("zoom"); pg.click("#zoom-in")
        check(ev("zoom") == min(220, z + 10) and pg.inner_text("#zoomval") == f"{ev('zoom')}%", "+ makes it bigger")
        pg.click("#menu-view"); pg.click("#menu-zoom-out")
        check(ev("zoom") == z, "View > Smaller makes it smaller again")

        # --- Start over asks first; saying no keeps everything
        pg.once("dialog", lambda d: d.dismiss())
        pg.click("#menu-edit"); pg.click("#btn-rebuild"); pg.wait_for_timeout(500)
        check(panel("a").get("grid") == "major", "Start over asks first; No keeps the changes")

        # --- left tool strip
        n = len(panel("a").get("texts") or [])
        ev("setSelection(['panel:a'])"); pg.click("#tool-label"); settle()
        check(len(panel("a").get("texts") or []) == n + 1, "tool strip: Label adds one")
        check(ev("document.getElementById('toolstrip').classList.contains('wide')")
              and shown("#tool-label .tl") and pg.inner_text("#tool-label") == "Words",
              "tool strip starts with the names showing")
        pg.click("#tool-expand")
        check(not shown("#tool-label .tl"), "the bottom button folds it to pictures only")
        pg.reload(); pg.wait_for_function("document.getElementById('status').dataset.ready === '1'")
        check(not shown("#tool-label .tl"), "…and it's remembered")
        pg.click("#tool-expand")
        check(shown("#tool-label .tl"), "…and shows them again")

        # --- bottom bar
        size = disk()["size_in"]
        check(pg.inner_text("#sb-size") == f"{size[0]:g} × {size[1]:g} in", f"bottom bar shows the size ({pg.inner_text('#sb-size')})")
        ev("setSelection(['a_meas'])")
        check(pg.inner_text("#sb-picked").startswith("Picked:"), "…and what's picked")
        ev("setSelection([])")
        check(pg.inner_text("#sb-picked") == "Nothing picked", "…or that nothing is")
        pg.wait_for_timeout(400)  # the figure re-fits once the numbers close
        box = ev("(() => { const g = geometry.panels.a; const r = svgEl.getBoundingClientRect();"
                 " const k = r.width / geometry.width; return [r.left + (g.bbox[0] + g.bbox[2] / 2) * k,"
                 " r.top + (g.bbox[1] + g.bbox[3] / 2) * k]; })()")
        pg.mouse.move(box[0], box[1])
        xy = pg.inner_text("#sb-xy"); g = ev("geometry.panels.a")
        try:
            parts = xy.split(); x, y = float(parts[parts.index("x") + 1]), float(parts[parts.index("y") + 1])
            mid_ok = (abs(x - sum(g["xlim"]) / 2) < abs(g["xlim"][1] - g["xlim"][0]) * 0.02
                      and abs(y - sum(g["ylim"]) / 2) < abs(g["ylim"][1] - g["ylim"][0]) * 0.02)
        except (ValueError, IndexError):
            mid_ok = False
        check(mid_ok, f"pointer in the middle of plot (a) reads the middle of its axes ({xy})")
        pg.mouse.move(5, 5)
        check(pg.inner_text("#sb-xy") == "", "…and clears when the pointer leaves")
        pg.click("#sb-list")
        check(shown("#element-list") and pg.inner_text("#sb-list") == "▾ List", "▴ List opens the list of everything")
        pg.click("#sb-list")
        check(not shown("#dock"), "…and closes it")

        # --- Help
        pg.click("#btn-help")
        check(shown("#help-pop") and "Add" in pg.inner_text("#help-pop"), "Help shows how to use it")
        pg.keyboard.press("Escape")
        check(not errs, f"no page errors {errs}")
        br.close()
finally:
    rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
sys.exit(1 if fails else 0)
