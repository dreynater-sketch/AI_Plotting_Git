"""Measure what an AI figure costs: run the assistant on fixed prompts with
this computer's storage and print every step's tokens, time and cost.
Spends real money (about $0.10-0.30 per prompt on Opus 5).

    python tools/ai_bench.py <tag> [prompt ...]     # circle, damped, rbs (default: circle, damped)
    FIGFORGE_AI_MODEL=claude-sonnet-5 python tools/ai_bench.py sonnet circle

Projects are kept as figures/bench_<tag>_<prompt> to look at afterwards;
a summary goes to tests/_out/ai_bench_<tag>.json.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from figforge import assistant  # noqa: E402
from figforge.project import Figure, LocalStore  # noqa: E402

PROMPTS = {
    # The user's own words from their first real AI figure (area_of_a_circle).
    "circle": "area of a circle by taking a square and progressively increasing number of sides "
              "that have equal length with radius r showing how a*a goes to pi r^2 doing this",
    "damped": "A damped harmonic oscillator: displacement against time for an underdamped, a "
              "critically damped and an overdamped case, with the decaying envelope of the "
              "underdamped one shown as dashed lines.",
    # The user's RBS overlay (rbs_overlay), from the folder they uploaded.
    "rbs": "Recreate the RUMP-style overlay in 'RBS example for chatgpt.png': normalized yield vs "
           "channel (about 150 to 1000) for 1A_middle, 1A_bottom_right, 1A_bottom_left and 1B "
           "(sim.mac maps the sample names to the b_01xx.asc files). Put energy in MeV on the top "
           "axis using the conv calibration from sim.mac. Label the O, Cu, NbSn and Ta surface "
           "edges. Serif font and black/red/green/blue line styles like the example.",
}
# The user's own words (Critical surface 3D): 3-D, physics models, no files.
PROMPTS["surface"] = "3d graph showing the surface of Jc, Tc, and Hc of nb3sn, Nb and rebco as a 2d surface in the 3d plot"
# The easy one: what most people ask for.
PROMPTS["sine"] = "Plot sin(x) for x from 0 to 2 pi."
# Follow-up messages in the same talk, run after the figure is made with
# "<prompt>+<edit>" (e.g. sine+easy_edit+hard_edit).
EDITS = {
    "easy_edit": "Make the line red and twice as thick, add a grid, and call the y axis 'Voltage (V)'.",
    "hard_edit": "Add a second panel underneath with the derivative cos(x), sharing the x axis, and "
                 "mark every zero crossing of sin(x) with a dot in both panels.",
}
# FIGFORGE_BENCH_ROUTE=1: sort each request onto a model like the server does.
ROUTED = os.environ.get("FIGFORGE_BENCH_ROUTE") == "1"
RBS_DIR = os.path.join(os.environ.get("TEMP", ""), "rbs")


def files_for(key):
    """The data files a prompt comes with, left out like the browser does
    (installers, slides, backups, the duplicate laptop copy)."""
    if key != "rbs":
        return []
    out = []
    for folder, _, names in os.walk(RBS_DIR):
        for n in names:
            path = os.path.join(folder, n)
            rel = os.path.relpath(path, RBS_DIR).replace(os.sep, "/")
            if "Rump from laptop" in rel or n.endswith((".exe", ".pptx", ".bak")):
                continue
            with open(path, "rb") as f:
                out.append({"name": rel, "data": f.read()})
    if not out:
        sys.exit(f"no RBS files in {RBS_DIR}")
    return sorted(out, key=lambda f: f["name"])


def _finish_turn(state, fig):
    t0 = time.time()
    while not state["done"]:
        state, spec, events = assistant.step(state, fig)
        if spec is not None:
            spec["rev"] = int(spec.get("rev", 0)) + 1
            fig.save_spec(spec)
        u = state["usage"][-1]
        print(f"  {u['step']:>2} {u['effort']:<6} {u['seconds']:>5.1f}s  in {u['input']:>6} "
              f"cw {u['cache_write']:>6} cr {u['cache_read']:>6} out {u['output']:>5}  "
              f"${u['cost_usd']:.4f}  {', '.join(t for t in u['tools'] if t)}")
    return state, time.time() - t0


def _snapshot(fig, tag, label):
    from figforge import render
    os.makedirs("tests/_out/bench_png", exist_ok=True)
    path = f"tests/_out/bench_png/{tag}_{label}.png"
    try:
        render.render_png(fig.load_spec(), fig.load_arrays(), path, dpi=80)
    except Exception as e:  # no figure made
        print("  (no picture:", e, ")")
        return None
    return path


def run(tag, key):
    key, *edits = key.split("+")
    store = LocalStore()
    name = f"bench_{tag}_{key}"
    if store.project_exists(name):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from _cleanup import rmtree
        rmtree(os.path.join("figures", name))
    fig = Figure(name, store)
    state = assistant.start(fig, files_for(key), [], PROMPTS[key])
    if ROUTED:
        assistant.route(state, PROMPTS[key], files_for(key))
        print("  sorted onto", state["model"])
    state, secs = _finish_turn(state, fig)
    fig.save_assistant(state)
    total = {"prompt": key, "model": state["model"], "steps": state["steps"], "views": state["views"],
             "seconds": round(secs, 1), "cost_usd": state["cost_usd"],
             "output_tokens": sum(u["output"] for u in state["usage"]), "reply": state["reply"],
             "picture": _snapshot(fig, tag, key)}
    print(f"  = {total['steps']} steps, {total['views']} pictures, {total['seconds']}s, "
          f"${total['cost_usd']:.4f}\n  {state['reply'][:300]}")
    total["edits"] = []
    for edit in edits:
        print(f"  follow-up {edit}:")
        before = state["cost_usd"]
        state = assistant.continue_chat(fig, state, EDITS[edit])
        if ROUTED:
            from figforge import ops
            assistant.route(state, EDITS[edit], figure=ops.describe(fig.load_spec(), fig.load_arrays()))
            print("  sorted onto", state["model"])
        state, secs = _finish_turn(state, fig)
        fig.save_assistant(state)
        total["edits"].append({"name": edit, "seconds": round(secs, 1),
                               "cost_usd": round(state["cost_usd"] - before, 5), "reply": state["reply"],
                               "picture": _snapshot(fig, tag, f"{key}_{edit}")})
        print(f"  = ${total['edits'][-1]['cost_usd']:.4f}, {secs:.0f}s\n  {state['reply'][:300]}")
    assistant.cleanup(state)
    return total


def main():
    assistant.load_key_from_dotenv()
    if not assistant.configured():
        sys.exit("ANTHROPIC_API_KEY is not set -- add it to .env first.")
    tag, keys = sys.argv[1], sys.argv[2:] or ["circle", "damped"]
    results = []
    for key in keys:
        print(f"{key} on {assistant.MODEL}:")
        results.append(run(tag, key))
    os.makedirs("tests/_out", exist_ok=True)
    with open(f"tests/_out/ai_bench_{tag}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
