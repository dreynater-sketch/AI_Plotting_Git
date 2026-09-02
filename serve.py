"""Start the local FigForge editor.

    python serve.py              http://127.0.0.1:8000
    python serve.py --port 8080
"""

import argparse
import webbrowser

from figforge.server import serve


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not args.no_browser:
        webbrowser.open(f"http://{args.host}:{args.port}/")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
