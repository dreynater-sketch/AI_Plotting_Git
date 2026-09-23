/* Runs figure.py in the browser, in a Web Worker, on Pyodide (CPython
 * compiled to WebAssembly). Nothing here touches the FigForge server: the
 * code executes on the viewer's own machine, inside the browser sandbox.
 * A worker (not the page) so a long or stuck script can be cancelled by
 * terminating it without freezing the editor. It must be a module worker:
 * current Pyodide refuses to start in a classic one.
 *
 * Messages in:  {type: 'run', code, npz: ArrayBuffer}
 * Messages out: {type: 'status', text} | {type: 'result', ok, out, images, errLine}
 */

const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/';
let ready = null;

function status(text) { postMessage({ type: 'status', text }); }

async function boot() {
  status('Downloading Python (first run only, ~30 MB)…');
  const { loadPyodide } = await import(PYODIDE + 'pyodide.mjs');
  const py = await loadPyodide({ indexURL: PYODIDE });
  status('Loading numpy + matplotlib…');
  await py.loadPackage(['numpy', 'matplotlib']);
  py.FS.mkdirTree('/project/data');
  return py;
}

// Mirrors how figure.py runs on a real machine: cwd and __file__ in the
// project folder, data/curves.npz beside it. Every open figure is returned
// as a PNG; stdout/stderr are captured together, in order.
const HARNESS = `
import base64, io, os, sys, traceback, warnings
os.environ["MPLBACKEND"] = "Agg"
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
warnings.filterwarnings("ignore", message=".*non-interactive.*")
# Pyodide's matplotlib 3.10 warns from inside its own tight_layout about
# float geometry -- noise about matplotlib, not the user's script.
warnings.filterwarnings("ignore", message=".*parameter as float was deprecated.*")
plt.close("all")
os.chdir("/project")
_buf = io.StringIO()
_saved = sys.stdout, sys.stderr
sys.stdout = sys.stderr = _buf
_ok, _err_line = True, None
try:
    _src = open("/project/figure.py", encoding="utf-8").read()
    exec(compile(_src, "figure.py", "exec"),
         {"__name__": "__main__", "__file__": "/project/figure.py"})
except SystemExit:
    pass
except BaseException as _e:
    _ok = False
    traceback.print_exc()
    _tb = _e.__traceback__
    if isinstance(_e, SyntaxError) and _e.filename == "figure.py":
        _err_line = _e.lineno
    while _tb is not None:
        if _tb.tb_frame.f_code.co_filename == "figure.py":
            _err_line = _tb.tb_lineno
        _tb = _tb.tb_next
finally:
    sys.stdout, sys.stderr = _saved
_images = []
for _n in plt.get_fignums():
    _b = io.BytesIO()
    plt.figure(_n).savefig(_b, format="png", dpi=110, facecolor="white")
    _images.append(base64.b64encode(_b.getvalue()).decode())
plt.close("all")
[_ok, _buf.getvalue(), _images, _err_line]
`;

onmessage = async (e) => {
  const msg = e.data;
  if (msg.type !== 'run') return;
  try {
    ready = ready || boot();
    const py = await ready;
    status('Running…');
    py.FS.writeFile('/project/data/curves.npz', new Uint8Array(msg.npz));
    py.FS.writeFile('/project/figure.py', msg.code);
    const res = py.runPython(HARNESS).toJs();
    postMessage({ type: 'result', ok: res[0], out: res[1], images: res[2], errLine: res[3] });
  } catch (err) {
    ready = null;  // a failed boot (e.g. offline) should retry next time
    postMessage({ type: 'result', ok: false, out: String(err && err.message || err), images: [], errLine: null });
  }
};
