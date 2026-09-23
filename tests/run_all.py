"""Run FigForge's test suites.

    python tests/run_all.py            everything that runs offline (~5 min)
    python tests/run_all.py ops csv    only suites whose name contains a word
    python tests/run_all.py --live     also the tests/live suites (need .env)

Needs the dev extras once:  pip install -r requirements-dev.txt
                            (Playwright drives the installed Microsoft Edge)

The browser suites start their own FigForge server: a local one on :8765
for the editor tests, and hosted-mode ones on :8010 backed by
tests/mock_supabase.py (:8020) for accounts, invites and the code editor.
They work in throwaway zz_* projects and delete them afterwards; the
first in-browser Run downloads Python (Pyodide) from jsDelivr.

tests/live/ talks to the real Supabase project and a deployed Vercel URL
using the key in .env; see each file's docstring. They create temporary
accounts and delete them again.
"""

import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8765

SUITES = [
    # (file, needs the local server)
    ("test_codesync.py", False),
    ("test_ops.py", False),
    ("test_undo_delete.py", True),
    ("test_box.py", True),
    ("test_pad.py", True),
    ("test_csv_import.py", True),
    ("test_code_editor.py", True),
    ("test_sync_e2e.py", True),
    ("test_accounts.py", False),
    ("test_invites.py", False),
    ("test_code_hosted.py", False),
]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    suites = [s for s in SUITES if not args or any(a in s[0] for a in args)]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SUPABASE_", "FIGFORGE_"))}  # local mode for the local server
    env.update(FF_BASE=f"http://127.0.0.1:{PORT}", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

    server = None
    if any(needs for _, needs in suites):
        server = subprocess.Popen([sys.executable, "serve.py", "--no-browser", "--port", str(PORT)],
                                  cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/auth/me")
                break
            except Exception:
                time.sleep(0.2)

    results = []
    try:
        runs = [(os.path.join(HERE, f), f) for f, _ in suites]
        if "--live" in sys.argv:
            runs += [(os.path.join(HERE, "live", f), "live/" + f)
                     for f in sorted(os.listdir(os.path.join(HERE, "live"))) if f.startswith("test_")]
        for path, name in runs:
            t = time.time()
            proc = subprocess.run([sys.executable, path], cwd=ROOT, env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=1200)
            out = proc.stdout + proc.stderr
            fails = [l for l in out.splitlines() if l.startswith("FAIL")]
            passes = sum(l.startswith("PASS") for l in out.splitlines())
            m = re.search(r"(\d+) failure\(s\)", out)
            ok = proc.returncode == 0 and m and m.group(1) == "0"
            results.append((name, ok, passes, time.time() - t))
            print(f"{'ok  ' if ok else 'FAIL'} {name:28s} {passes:3d} checks  {time.time() - t:5.1f}s")
            if not ok:
                for line in fails or out.strip().splitlines()[-12:]:
                    print("       " + line)
    finally:
        if server:
            server.terminate()
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} suites passed, "
          f"{sum(r[2] for r in results)} checks")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
