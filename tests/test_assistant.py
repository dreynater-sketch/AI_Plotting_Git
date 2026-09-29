"""The assistant's step loop with a scripted fake Claude: no API key, no
cost. Checks create_figure -> view_figure -> an edit -> the final reply, the
error paths Claude gets back, the limits, and that the tool schemas stay
inside strict mode's 16-union-parameter cap."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os, shutil, sys, types
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)

from figforge import assistant
from figforge.project import Figure, LocalStore

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)


class Block(types.SimpleNamespace):
    def to_dict(self):
        return dict(vars(self))


def reply(*blocks, stop="tool_use"):
    usage = types.SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return types.SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


def tool(name, **inp):
    tool.n += 1
    return Block(type="tool_use", id=f"toolu_{tool.n}", name=name, input=inp)
tool.n = 0


class FakeClient:
    def __init__(self, script):
        self.script, self.seen = list(script), []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.seen.append(kw)
        return self.script.pop(0)


PANEL = dict(x_column=0, y_columns=[1, 2], plot_as="scatter", title="", x_label="Time ($\\mu$s)",
             y_label="Counts", x_scale="linear", y_scale="log")
CSV = "t,a,b,note\n" + "\n".join(f"{i},{100 * 0.9 ** i:.3f},{50 * 0.95 ** i:.3f},x" for i in range(20)) + "\n"

P = "zz_assistant_test"
store = LocalStore()
if store.project_exists(P):
    shutil.rmtree(f"figures/{P}")
fig = Figure(P, store)
fig.save_source_csv(CSV)

state = assistant.start(CSV, "decay.csv", "Compare a and b, log y.")
first = state["messages"][0]["content"]
check('"name": "note"' in first and "decay.csv" in first and "Compare a and b" in first,
      "first message: column summary, file name and the idea")

fake = FakeClient([
    reply(tool("set_text", element_id="a__title", text="too early")),
    reply(tool("create_figure", figure_title="Decay", panels=[PANEL])),
    reply(tool("create_figure", figure_title="Again", panels=[PANEL]),
          tool("view_figure")),
    reply(tool("style_series", series_id="a_s0", color="#D62728", line_width=None,
               line_style="keep", marker="keep", marker_size=None, opacity=None)),
    reply(Block(type="text", text="Both samples decay; a falls faster."), stop="end_turn"),
])
assistant._client = lambda: fake


def save(spec):
    if spec is not None:
        fig.save_spec(spec)


state, spec, ev = assistant.step(state, fig, CSV)
res = state["messages"][-1]["content"][0]
check(res.get("is_error") and "create_figure first" in res["content"] and spec is None,
      "an edit before create_figure comes back as an error")

state, spec, ev = assistant.step(state, fig, CSV); save(spec)
p = spec["panels"][0] if spec else {}
check(spec and spec["suptitle"]["text"] == "Decay" and p["yscale"] == "log" and len(p["series"]) == 2
      and p["xlabel"]["text"] == "Time ($\\mu$s)" and p["title"]["text"] == "",
      "create_figure builds the requested panel (log y, labels, empty title)")
check(ev == ["Drew the figure"], f"progress event ({ev})")
check(spec["source"]["kind"] == "csv-panels", "the figure remembers how to be rebuilt")

state, spec, ev = assistant.step(state, fig, CSV); save(spec)
again, view = state["messages"][-1]["content"]
check(again.get("is_error") and "already exists" in again["content"], "a second create_figure is refused")
check(view["content"][0]["type"] == "image" and view["content"][0]["source"]["media_type"] == "image/png"
      and state["views"] == 1, "view_figure returns a PNG picture")

state, spec, ev = assistant.step(state, fig, CSV); save(spec)
check(spec and spec["panels"][0]["series"][0]["style"]["color"].lower() == "#d62728"
      and spec["panels"][0]["series"][0]["style"].get("marker") == "o",
      "an edit applies; \"keep\" leaves the marker alone")

state, spec, ev = assistant.step(state, fig, CSV)
check(state["done"] and state["reply"] == "Both samples decay; a falls faster.", "final text is the reply")
check(abs(state["cost_usd"] - 5 * (1000 * 5 + 200 * 25) / 1e6) < 1e-6, f"cost adds up ({state['cost_usd']})")
kw = fake.seen[-1]
check(kw["model"] == "claude-opus-5" and kw["fallbacks"] == "default"
      and "server-side-fallback-2026-07-01" in kw["betas"], "request: model + refusal fallback")
check(all(m["role"] in ("user", "assistant") for m in kw["messages"]) and kw["messages"][0]["role"] == "user",
      "history alternates user / assistant")


def unions(s):
    n = 0
    if isinstance(s, dict):
        n += "anyOf" in s or isinstance(s.get("type"), list)
        n += sum(unions(v) for v in s.get("properties", {}).values())
        n += unions(s.get("items"))
    return n
total = sum(unions(t["input_schema"]) for t in assistant.TOOLS)
check(total <= 16, f"strict tools stay under the 16 union-parameter cap ({total})")
check(all(t.get("strict") for t in assistant.TOOLS), "every tool is strict")

# Limits: a session stops itself.
st = assistant.start(CSV, "decay.csv", "x")
st["steps"] = assistant.MAX_STEPS - 1
assistant._client = lambda: FakeClient([reply(tool("describe_figure"))])
st, _, _ = assistant.step(st, fig, CSV)
check(st["done"] and "maximum number of steps" in st["reply"], "stops at the step limit")

shutil.rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
