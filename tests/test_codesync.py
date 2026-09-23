"""Unit tests: figure.py edits -> spec (figforge.codesync)."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, subprocess, sys
ROOT = ROOT_DIR; sys.path.insert(0, ROOT)
import os; os.chdir(ROOT)
import numpy as np
from figforge import codegen, codesync, render
npz = np.load("figures/qcircle/data/curves.npz"); arrays = {k: npz[k] for k in npz.files}; keys = set(arrays)
spec = json.loads(subprocess.check_output(["git", "show", "6a86d26:figures/qcircle/spec.json"]))
base = codegen.generate(spec)
fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
def texts(sp, pid): return {t["id"]: t for t in next(p for p in sp["panels"] if p["id"] == pid)["texts"]}
def run(code):
    new, rep = codesync.apply(code, spec, keys)
    render.render(new, arrays, preview=True)
    return new, rep
def element(code, gid, start_marker):
    i = code.index(f"gid='{gid}'")
    st = code.rfind(start_marker, 0, i)
    end = code.index("\n", i)
    return st, end

new, rep = run(base)
check(new == spec and not any(rep.values()), "unedited code: no changes at all")

# 1. label text, size, colour
st, end = element(base, "a_R", "_t = ax.text(")
seg = base[st:end].replace("'$R=0.075$'", "'$R=0.080$'").replace("fontsize=11", "fontsize=15").replace("'#2E8B57'", "'#ff0000'")
new, rep = run(base[:st] + seg + base[end:])
t = texts(new, "a")["a_R"]
check((t["text"], t["size"], t["color"]) == ("$R=0.080$", 15, "#ff0000"), f"label text/size/colour applied {t['text'], t['size'], t['color']}")
check(rep["changed"] == ["(a) “$R=0.080$”: colour, size, text"], f"report names exactly that: {rep['changed']}")

# 2. limits + figure size
new, rep = run(base.replace("ax.set_xlim(-40, 40)", "ax.set_xlim(-30, 30)").replace("figsize=(16.2, 5.35)", "figsize=(15.0, 5.0)"))
check(new["panels"][2]["xlim"] == [-30, 30] and new["size_in"] == [15.0, 5.0], "axis limits + figure size applied")

# 3. delete a label
st, end = element(base, "b_beta2", "_t = ax.text(")
new, rep = run(base[:st] + base[end + 1:])
check("b_beta2" not in texts(new, "b") and rep["removed"] == ["(b) text “$" + chr(92) + "beta_2=0.24$”"], f"deleting its lines removes it ({rep['removed']})")

# 4. add a new label
a = base.index("ax = axes[0, 1]") + len("ax = axes[0, 1]")
new, rep = run(base[:a] + "\nax.text(0.02, 0.05, 'Hello', fontsize=9, color='blue', transform=ax.transAxes)" + base[a:])
tb = texts(new, "b")
hello = [t for t in tb.values() if t["text"] == "Hello"]
check(hello and hello[0]["id"] not in texts(spec, "b") and hello[0]["coords"] == "axes",
      f"new ax.text is a new label with a fresh id ({hello and hello[0]['id']})")
check(all(tb[i] == t for i, t in texts(spec, "b").items()), "…and every existing label in (b) is unchanged")
check(rep["added"] == ["(b) text “Hello”"] and not rep["removed"] and not rep["changed"], f"report: {rep['added']}")

# 5. curve colour
st, end = element(base, "a_fit", "ax.plot(")
seg = base[st:end]
import re
seg2 = re.sub(r"color='[^']*'", "color='#00aa00'", seg, count=1)
new, rep = run(base[:st] + seg2 + base[end:])
fit = [s for s in new["panels"][0]["series"] if s["id"] == "a_fit"][0]
check(fit["style"]["color"] == "#00aa00", "curve colour applied")

# 6. loops / variables skipped, other edits still applied
new, rep = run(base.replace("ax.set_xlim(-40, 40)", "ax.set_xlim(-35, 35)") + "\nfor k in range(3):\n    print(k)\nsz = 20\n")
check(new["panels"][2]["xlim"] == [-35, 35] and [s["reason"] for s in rep["skipped"]] ==
      ["loops aren't reflected in the figure", "variables aren't reflected in the figure"], "loop + variable skipped, with reasons")

# 7. non-literal value on a label -> label left as it was
st, end = element(base, "a_C", "_t = ax.text(")
seg = base[st:end].replace("fontsize=11", "fontsize=sz")
new, rep = run("sz = 20\n" + base[:st] + seg + base[end:])
check(texts(new, "a").get("a_C") == texts(spec, "a")["a_C"] and not rep["removed"],
      f"label whose line can't be read keeps its settings (removed={rep['removed']}, changed={rep['changed']})")

# 8. arrow end point
st, end = element(base, "a_R_arrow", "ax.add_patch(")
seg = base[st:end]
seg2 = seg.replace("(-0.06708261156308878, 0.00616816847304507)", "(0.0, 0.0)")
new, rep = run(base[:st] + seg2 + base[end:])
arrow = [x for x in new["panels"][0]["arrows"] if x["id"] == "a_R_arrow"][0]
check(arrow["p1"] == [0.0, 0.0] and arrow["p0"] == [-0.14218655750048398, 0.013073889355671244], "arrow end moved, start untouched")

# 9. moving a label's line to another panel moves the label
st, end = element(base, "a_C", "_t = ax.text(")
line = base[st:end]
rest = base[:st] + base[end + 1:]
c = rest.index("ax = axes[0, 2]") + len("ax = axes[0, 2]")
new, rep = run(rest[:c] + "\n" + line + rest[c:])
check("a_C" in texts(new, "c") and "a_C" not in texts(new, "a"), "moving a label's code under another panel moves it there")

# 10. syntax error, suptitle removal, panel-count change
try:
    codesync.apply(base + "\ndef broken(:\n", spec, keys); check(False, "syntax error raised")
except SyntaxError as e:
    check(e.lineno > 150, "syntax error reported with its line")
new, rep = run("\n".join(l for l in base.splitlines() if not l.startswith("fig.suptitle")) + "\n")
check(new["suptitle"]["text"] == "" and "figure title" in rep["changed"], "deleting fig.suptitle blanks the title")
new, rep = run(base.replace("plt.subplots(1, 3,", "plt.subplots(1, 4,"))
check(new["panels"] == spec["panels"] and "number of panels" in rep["skipped"][0]["reason"], "changing the panel count is refused, nothing else breaks")
new, rep = run(base.replace("'b_fit'", "'b_fit'").replace("D[\"b_fit_x\"]", "D[\"nope\"]") if 'D["b_fit_x"]' in base else base.replace('D["', 'D["zz', 1))
check(any("no D[" in s["reason"] for s in rep["skipped"]), "a missing data key is refused, not rendered")
print(len(fails), "failure(s)")
