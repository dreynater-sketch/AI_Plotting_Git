"""Local-only editor server. Stdlib http.server, no dependencies, no network.

    GET  /                     the editor
    GET  /api/figures          names of available figures
    GET  /api/figure/<name>    {spec, svg, geometry}
    POST /api/figure/<name>    {spec} -> re-render -> {spec, svg, geometry}
    GET  /api/code/<name>      standalone figure.py (text/plain)
    GET  /api/png/<name>       rendered PNG at the spec's export dpi

Binds 127.0.0.1 only. Nothing here talks to any external service; there is no
AI in this build.
"""

import json
import mimetypes
import os
import posixpath
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from figforge import codegen, render
from figforge.project import ROOT, Figure, list_figures

WEB = os.path.join(ROOT, "web")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "FigForge/0.1"

    # ------------------------------------------------------------ helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _error(self, code, msg):
        self._json({"error": msg}, code)

    def _figure(self, name):
        """Resolve a figure name, refusing anything that isn't a plain name."""
        if not NAME_RE.match(name or ""):
            return None
        fig = Figure(name)
        return fig if fig.exists() else None

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path == "/api/figures":
            return self._json({"figures": list_figures()})

        if path.startswith("/api/figure/"):
            fig = self._figure(path[len("/api/figure/"):])
            if not fig:
                return self._error(404, "unknown figure")
            return self._json(self._payload(fig, fig.load_spec()))

        if path.startswith("/api/code/"):
            fig = self._figure(path[len("/api/code/"):])
            if not fig:
                return self._error(404, "unknown figure")
            return self._send(200, codegen.generate(fig.load_spec()),
                              "text/plain; charset=utf-8")

        if path.startswith("/api/png/"):
            fig = self._figure(path[len("/api/png/"):])
            if not fig:
                return self._error(404, "unknown figure")
            spec = fig.load_spec()
            png = os.path.join(fig.dir, "figure.png")
            render.render_png(spec, fig.load_arrays(), png, spec.get("dpi", 200))
            with open(png, "rb") as f:
                return self._send(200, f.read(), "image/png")

        return self._static(path)

    # --------------------------------------------------------------- POST
    def do_POST(self):
        path = unquote(urlparse(self.path).path)

        if path.startswith("/api/rebuild/"):
            fig = self._figure(path[len("/api/rebuild/"):])
            if not fig:
                return self._error(404, "unknown figure")
            return self._rebuild(fig)

        if not path.startswith("/api/figure/"):
            return self._error(404, "not found")
        fig = self._figure(path[len("/api/figure/"):])
        if not fig:
            return self._error(404, "unknown figure")

        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            spec = body["spec"]
        except (ValueError, KeyError, TypeError) as e:
            return self._error(400, f"bad request: {e}")

        try:
            spec["rev"] = int(spec.get("rev", 0)) + 1
            payload = self._payload(fig, spec)
        except Exception as e:  # a bad edit shouldn't kill the server
            return self._error(422, f"render failed: {type(e).__name__}: {e}")

        # Only persist once the spec proved it can render.
        fig.save_spec(spec)
        with open(fig.svg_path, "w", encoding="utf-8") as f:
            f.write(payload["svg"])
        with open(fig.py_path, "w", encoding="utf-8") as f:
            f.write(codegen.generate(spec))
        return self._json(payload)

    def _rebuild(self, fig):
        """Throw away layout edits and regenerate the spec from raw data."""
        from figforge import analyze, spec_builder
        arrays, derived = analyze.analyze(fig.csv_path)
        fig.save_arrays(arrays)
        spec = spec_builder.build_spec(derived)
        fig.save_spec(spec)
        payload = self._payload(fig, spec)
        with open(fig.svg_path, "w", encoding="utf-8") as f:
            f.write(payload["svg"])
        with open(fig.py_path, "w", encoding="utf-8") as f:
            f.write(codegen.generate(spec))
        return self._json(payload)

    # -------------------------------------------------------------- parts
    def _payload(self, fig, spec):
        svg, geom = render.render(spec, fig.load_arrays())
        return {"spec": spec, "svg": svg, "geometry": geom}

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath.normpath(path).lstrip("/")
        full = os.path.join(WEB, *rel.split("/"))
        # normpath already stripped "..", but be explicit about the jail.
        if not os.path.abspath(full).startswith(os.path.abspath(WEB) + os.sep):
            return self._error(403, "forbidden")
        if not os.path.isfile(full):
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)


def serve(host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"FigForge editor:  http://{host}:{port}/")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
