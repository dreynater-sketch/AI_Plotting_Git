"""Unit tests for figforge.ops (assistant tool definitions + dispatcher)."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
OUT_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_out")
_os.makedirs(OUT_DIR, exist_ok=True)
import json, os, subprocess, sys
ROOT = ROOT_DIR; os.chdir(ROOT); sys.path.insert(0, ROOT)
import numpy as np
from figforge import ops, render, csvimport

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)


def strict_ok(sch, path="root"):
    if sch.get("type") == "object":
        assert sch.get("additionalProperties") is False, path
        assert sorted(sch["required"]) == sorted(sch["properties"]), path
        for k, v in sch["properties"].items():
            strict_ok(v, f"{path}.{k}")
    for alt in sch.get("anyOf", []):
        strict_ok(alt, path)
    for bad in ("minimum", "maximum", "minLength", "maxLength", "multipleOf"):
        assert bad not in sch, f"{path}: {bad}"
    return True


check(all(t["strict"] and strict_ok(t["input_schema"], t["name"]) for t in ops.TOOLS),
      f"{len(ops.TOOLS)} tools: strict, closed objects, all properties required, no numeric/string limits")
check(len({t["name"] for t in ops.TOOLS}) == len(ops.TOOLS) and all(len(t["description"]) > 30 for t in ops.TOOLS),
      "unique names, real descriptions")
check(json.loads(json.dumps(ops.TOOLS)) == ops.TOOLS, "tool definitions are plain JSON")

spec = json.loads(subprocess.check_output(["git", "show", "6a86d26:figures/qcircle/spec.json"]))
original = json.dumps(spec, sort_keys=True)
npz = np.load("figures/qcircle/data/curves.npz"); arrays = {k: npz[k] for k in npz.files}
d = ops.describe(spec, arrays)
check(len(d["panels"]) == 3 and d["panels"][0]["labels"][0]["id"] == "a_beta1" and d["panels"][0]["curves"][0]["points"] > 100,
      "describe_figure: panels, labels with ids, curves with point counts + ranges")
check(len(json.dumps(d)) < 12000, f"description is compact ({len(json.dumps(d))} chars)")

calls = [
    {"name": "set_text", "input": {"element_id": "a_R", "text": "$R = 0.08$"}},
    {"name": "style_text", "input": {"element_id": "a__title", "size": 18, "color": "navy"}},
    {"name": "move_element", "input": {"element_id": "a_C", "x": 0.5, "y": 0.5, "coords": "axes"}},
    {"name": "add_label", "input": {"panel_id": "b", "text": "note", "x": 0.1, "y": 0.9, "coords": "axes", "size": None, "color": None}},
    {"name": "set_axis", "input": {"panel_id": "c", "axis": "x", "min": -30, "max": 30, "scale": None, "tick_size": 12}},
    {"name": "style_series", "input": {"series_id": "c_fit", "color": "#aa0000", "line_width": 2.5, "line_style": "--", "marker": None, "marker_size": None, "opacity": 0.8}},
    {"name": "set_legend", "input": {"panel_id": "a", "visible": True, "location": "upper left", "font_size": 9, "frame": True}},
    {"name": "delete_element", "input": {"element_id": "b_dbeta"}},
    {"name": "set_figure_size", "input": {"width_in": 15, "height_in": 5}},
]
new, res, changed = ops.apply(spec, calls, arrays)
check(changed and all(not r["is_error"] for r in res), f"all 9 edit tools apply {[r['content'] for r in res if r['is_error']]}")
render.render(new, arrays, preview=True)
check(True, "edited figure renders")
check(json.dumps(spec, sort_keys=True) == original, "the input spec is never modified")
check(res[3]["content"] == "added label b_label1" and any(t["id"] == "b_label1" for t in new["panels"][1]["texts"]),
      "add_label returns the new id")

bad = [
    {"name": "set_text", "input": {"element_id": "nope", "text": "x"}},
    {"name": "style_text", "input": {"element_id": "a_R", "size": 500, "color": None}},
    {"name": "style_text", "input": {"element_id": "a_R", "size": None, "color": "not-a-colour"}},
    {"name": "set_axis", "input": {"panel_id": "a", "axis": "x", "min": None, "max": None, "scale": "log", "tick_size": None}},
    {"name": "set_text", "input": {"element_id": "a_R", "text": "$unbalanced"}},
    {"name": "move_element", "input": {"element_id": "a__title", "x": 1, "y": 1, "coords": "axes"}},
    {"name": "set_text", "input": {"element_id": "a_R"}},
    {"name": "rm_rf", "input": {}},
    {"name": "set_text", "input": {"element_id": "a_R", "text": "ok"}},
]
new2, res2, changed2 = ops.apply(spec, bad, arrays)
flags = [r["is_error"] for r in res2]
check(flags == [True] * 8 + [False], f"8 bad calls rejected with reasons; the valid call after them still applies {flags}")
for r in res2[:8]:
    print("      ", r["name"], "->", r["content"])
check(ops.apply(spec, [{"name": "describe_figure", "input": {}}], arrays)[2] is False, "describe_figure changes nothing")

cs, ca = csvimport.build("demo", "t,a,b\n0,1,2\n1,2,3\n2,3,5\n", 0, [1, 2], "line", "demo.csv")
n3, r3, c3 = ops.apply(cs, [{"name": "style_series", "input": {"series_id": "a_s1", "color": "green", "line_width": None,
                                                                 "line_style": ":", "marker": "o", "marker_size": 6, "opacity": None}}], ca)
render.render(n3, ca, preview=True)
check(c3 and not r3[0]["is_error"], "tools work on CSV-made figures")
print(len(fails), "failure(s)")
