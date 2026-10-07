"""The assistant's step loop with a scripted fake Claude: no API key, no
cost. Checks the upload (several files -> one zip, a tidy CSV kept as a
table, an example image shown to Claude), a sandbox-made table arriving,
create_figure -> view_figure -> edits (incl. a second axis) -> the final
reply, the error paths Claude gets back, the limits, and that the tool
schemas stay inside strict mode's 16-union-parameter cap."""
import os as _os
ROOT_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import io, os, shutil, sys, types, zipfile
from _cleanup import rmtree
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)

from figforge import assistant
from figforge.project import Figure, LocalStore

fails = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)


class Block(types.SimpleNamespace):
    def to_dict(self):
        return dict(vars(self))


def reply(*blocks, stop="tool_use", container="cntr_1"):
    usage = types.SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return types.SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage,
                                 container=types.SimpleNamespace(id=container))


def tool(name, **inp):
    tool.n += 1
    return Block(type="tool_use", id=f"toolu_{tool.n}", name=name, input=inp)
tool.n = 0


def sandbox_wrote(file_id):
    """What a code-execution run that saved a file to $OUTPUT_DIR looks like."""
    return [Block(type="server_tool_use", id="srvtoolu_1", name="bash_code_execution",
                  input={"command": "python make_table.py"}),
            Block(type="bash_code_execution_tool_result", tool_use_id="srvtoolu_1",
                  content={"type": "bash_code_execution_result", "stdout": "ok", "stderr": "",
                           "return_code": 0,
                           "content": [{"type": "bash_code_execution_output", "file_id": file_id}]})]


SPECTRA = "channel,energy_MeV,1A_middle,1B\n" + "\n".join(
    f"{c},{(1.91758 * c + 72.93) / 1000:.5f},{100 + (c % 7)},{90 + (c % 5)}" for c in range(150, 400)) + "\n"


class FakeClient:
    def __init__(self, script):
        self.script, self.seen, self.uploads, self.deleted = list(script), [], [], []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))
        self.files = types.SimpleNamespace(upload=self.upload, retrieve_metadata=self.meta,
                                           download=self.download, delete=self.deleted.append)

    def create(self, **kw):
        self.seen.append(kw)
        return self.script.pop(0)

    def upload(self, file):
        name, fh = file
        self.uploads.append((name, fh.read()))
        return types.SimpleNamespace(id="file_in")

    def meta(self, fid):
        return types.SimpleNamespace(filename={"file_tab": "spectra.csv", "file_png": "check.png"}[fid],
                                     size_bytes=len(SPECTRA))

    def download(self, fid):
        return io.BytesIO(SPECTRA.encode())


PANEL = dict(x_column="channel", y_columns=["1A_middle", "1B"], plot_as="line", title="",
             x_label="Channel", y_label="Normalized yield", x_scale="linear", y_scale="linear")

P = "zz_assistant_test"
store = LocalStore()
if store.project_exists(P):
    rmtree(f"figures/{P}")
fig = Figure(P, store)

fake = FakeClient([
    reply(tool("set_text", element_id="a__title", text="too early")),
    reply(tool("create_figure", table="nothing.csv", figure_title="", panels=[PANEL])),
    reply(*sandbox_wrote("file_tab"), *sandbox_wrote("file_png"), stop="pause_turn"),
    reply(tool("create_figure", table="spectra.csv", figure_title="RBS", panels=[PANEL])),
    reply(tool("create_figure", table="spectra.csv", figure_title="Again", panels=[PANEL]),
          tool("view_figure")),
    reply(tool("set_second_axis", panel_id="a", side="top", show=True, label="Energy (MeV)",
               scale=0.00191758, offset=0.07293023),
          tool("style_series", series_id="a_s1", color="", line_width=None,
               line_style="--", marker="keep", marker_size=None, opacity=None),
          tool("add_label", panel_id="a", text="Cu", x=300, y=50, coords="data", size=None, color="")),
    reply(Block(type="text", text="The two spectra overlap; energy is on top."), stop="end_turn"),
])
assistant._client = lambda: fake

files = [{"name": "RBS/04152025/b_0106.asc", "data": b"<<DATA>>\n1\n2\n"},
         {"name": "RBS/notes.csv", "data": b"x,y\n1,2\n2,4\n"},
         {"name": "RBS/example.png", "data": b"\x89PNG\r\n\x1a\nfake"}]
state = assistant.start(fig, files, [{"name": "RBS/rump_setup.exe", "size": 9_000_000,
                                      "reason": "program or installer"}], "Overlay the spectra.")
name, payload = fake.uploads[0]
check(name == "inputs.zip" and sorted(zipfile.ZipFile(io.BytesIO(payload)).namelist())
      == sorted(f["name"] for f in files), "several files go to the sandbox as one zip")
first = state["messages"][0]["content"]
text = " ".join(b.get("text", "") for b in first)
check("b_0106.asc" in text and "rump_setup.exe" in text and "installer" in text
      and "Overlay the spectra." in text, "first message lists the files, what was left out, and the idea")
check(any(b["type"] == "container_upload" and b["file_id"] == "file_in" for b in first),
      "the zip is attached to the sandbox")
check(any(b["type"] == "image" for b in first), "an example picture is shown to Claude")
check("notes.csv" in state["tables"] and fig.load_table("notes.csv").startswith("x,y"),
      "an uploaded tidy CSV is available as a table straight away")


def run():
    global state
    state, spec, ev = assistant.step(state, fig)
    if spec is not None:
        fig.save_spec(spec)
    return spec, ev


spec, ev = run()
res = state["messages"][-1]["content"][0]
check(res.get("is_error") and "create_figure first" in res["content"], "an edit before create_figure is an error")

spec, ev = run()
res = state["messages"][-1]["content"][0]
check(res.get("is_error") and "no table 'nothing.csv'" in res["content"], "an unknown table is an error")

spec, ev = run()
check(state["tables"].get("spectra.csv") and "check.png" not in state["tables"]
      and fig.load_table("spectra.csv") == SPECTRA, "a CSV saved in the sandbox arrives; a PNG doesn't")
check("Made a table: spectra.csv" in ev and not state["done"], f"pause_turn keeps going ({ev})")
check(state["container"] == "cntr_1", "the sandbox container is remembered")

spec, ev = run()
p = spec["panels"][0] if spec else {}
check(spec and spec["suptitle"]["text"] == "RBS" and [s["label"] for s in p["series"]] == ["1A_middle", "1B"]
      and p["xlabel"]["text"] == "Channel", "create_figure uses column names from the table")
check(fig.load_source_csv() == SPECTRA and spec["source"]["kind"] == "csv-panels",
      "the table is kept so Start over can rebuild")
check(fake.seen[-1].get("container") == "cntr_1", "later requests reuse the container")

spec, ev = run()
again, view = state["messages"][-1]["content"]
check(again.get("is_error") and "already exists" in again["content"], "a second create_figure is refused")
check(view["content"][0]["type"] == "image", "view_figure returns a PNG picture")

spec, ev = run()
p = spec["panels"][0]
check(p["top_axis"]["text"] == "Energy (MeV)" and p["top_axis"]["scale"] == 0.00191758,
      "set_second_axis adds the energy scale")
check(p["series"][1]["style"]["ls"] == "--" and p["texts"][0]["text"] == "Cu", "restyle + label applied")

spec, ev = run()
check(state["done"] and state["reply"].startswith("The two spectra"), "final text is the reply")
kw = fake.seen[-1]
check(kw["model"] == "claude-opus-5" and kw["fallbacks"] == "default"
      and any(t.get("type", "").startswith("code_execution") for t in kw["tools"]),
      "request: model, refusal fallback, sandbox tool")
assistant.cleanup(state)
check(set(fake.deleted) == {"file_in", "file_tab", "file_png"}, "Anthropic-side files are cleaned up")


def unions(s):
    n = 0
    if isinstance(s, dict):
        n += "anyOf" in s or isinstance(s.get("type"), list)
        n += sum(unions(v) for v in s.get("properties", {}).values())
        n += unions(s.get("items"))
    return n
custom = [t for t in assistant.TOOLS if "input_schema" in t]
total = sum(unions(t["input_schema"]) for t in custom)
check(total <= 16, f"strict tools stay under the 16 union-parameter cap ({total})")
check([t["name"] for t in custom if t.get("strict")] == ["create_figure"],
      "only create_figure is strict (the grammar size limit)")

# Limits: a session stops itself.
st = dict(state, done=False, steps=assistant.MAX_STEPS - 1)
last = FakeClient([reply(tool("describe_figure"))])
assistant._client = lambda: last
st, _, _ = assistant.step(st, fig)
check(last.seen[-1].get("tool_choice") == {"type": "none"}, "the last allowed turn is a no-tools wrap-up")
check(st["done"] and "maximum number of steps" in st["reply"], "stops at the step limit")

rmtree(f"figures/{P}")
print(len(fails), "failure(s)")
