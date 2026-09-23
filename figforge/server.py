"""Local-only editor server. Stdlib http.server, no dependencies, no network.

    GET  /                     the editor
    GET  /api/figures          names of available figures
    GET  /api/figure/<name>    {spec, svg, geometry}
    POST /api/figure/<name>    {spec} -> re-render -> {spec, svg, geometry}
    GET  /api/code/<name>      standalone figure.py (text/plain)
    GET  /api/png/<name>       rendered PNG at the spec's export dpi
    GET  /api/history/<name>   saved undo/redo stacks {undo: [spec...], redo: [...]}
    POST /api/history/<name>   {undo, redo} -> persist both stacks
    POST /api/project/duplicate  {from, name} -> copy a project ("Save as")
    POST /api/project/rename     {from, name} -> rename a project folder

Hosted only (accounts, see figforge/auth.py) -- every other /api/ route
then requires a signed-in user and works in that user's own project space:

    GET  /api/auth/me          {mode: "local"} | {mode: "online", user} | 401
    POST /api/auth/signup      {email, password, display_name}
    POST /api/auth/login       {email, password}
    POST /api/auth/session     {access_token, refresh_token}  (from email links)
    POST /api/auth/logout
    POST /api/auth/recover     {email} -> password-reset email
    POST /api/auth/profile     {display_name}
    POST /api/auth/password    {password}

Locally this binds 127.0.0.1 and talks to nothing external; there is no AI in
this build. The same Handler also runs as FigForge's Vercel function
(api/index.py), where projects live in a private Supabase Storage bucket
instead of figures/ -- see figforge/project.py. Vercel caps request and response bodies
at 4.5 MB and a full undo history can approach that, so /api/history travels
gzipped (X-Body-Encoding: gzip up, Content-Encoding: gzip down).
"""

import gzip
import http.cookies
import io
import json
import mimetypes
import os
import posixpath
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from figforge import codegen, render
from figforge.auth import AuthError, public_user
from figforge.project import AUTH, ROOT, STORE, Figure, list_figures

WEB = os.path.join(ROOT, "web")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HISTORY_MAX = 100  # per stack; matches the editor's HISTORY_MAX
SESSION_MAX_AGE = 30 * 24 * 3600  # cookies; Supabase's refresh token outlives the 1 h access token
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "FigForge/0.1"

    # ------------------------------------------------------------ helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for c in getattr(self, "_cookies", []):
            self.send_header("Set-Cookie", c)
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
        fig = Figure(name, self.store)
        return fig if fig.exists() else None

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        self._cookies = []
        if path == "/api/auth/me":
            return self._auth_me()
        if path.startswith("/api/") and not self._authorize():
            return

        if path == "/api/figures":
            return self._json({"figures": list_figures(self.store),
                               "storage": "local" if STORE.persistent_outputs else "online"})

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
            buf = io.BytesIO()
            render.render_png(spec, fig.load_arrays(), buf, spec.get("dpi", 200))
            return self._send(200, buf.getvalue(), "image/png")

        if path.startswith("/api/data/"):
            return self._series_data(path[len("/api/data/"):])

        if path.startswith("/api/history/"):
            fig = self._figure(path[len("/api/history/"):])
            if not fig:
                return self._error(404, "unknown figure")
            body = json.dumps(fig.load_history()).encode("utf-8")
            if "gzip" not in self.headers.get("Accept-Encoding", ""):
                return self._send(200, body)
            return self._send(200, gzip.compress(body, 6),
                              extra={"Content-Encoding": "gzip"})

        return self._static(path)

    def _series_data(self, rest):
        """The raw (x, y) values behind one series -- what a clicked curve's
        data table shows. Not folded into /api/figure: the spec stays small
        and the bulk arrays only cross the wire when a curve is actually
        selected (spec section 4, "data is referenced, not duplicated")."""
        parts = rest.split("/", 1)
        if len(parts) != 2:
            return self._error(400, "expected /api/data/<figure>/<series id>")
        fig_name, series_id = parts
        fig = self._figure(fig_name)
        if not fig or not NAME_RE.match(series_id):
            return self._error(404, "unknown figure or series")

        spec = fig.load_spec()
        arrays = fig.load_arrays()
        for p in spec["panels"]:
            for s in p.get("series", []):
                if s["id"] != series_id:
                    continue
                x = arrays[s["x"]] if isinstance(s["x"], str) else s["x"]
                y = arrays[s["y"]] if isinstance(s["y"], str) else s["y"]
                return self._json({
                    "id": series_id, "panel": p["id"], "label": s.get("label", ""),
                    "x": [float(v) for v in x], "y": [float(v) for v in y],
                })
        return self._error(404, "unknown series")

    # --------------------------------------------------------------- POST
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        self._cookies = []
        if path.startswith("/api/auth/"):
            return self._auth_api(path[len("/api/auth/"):])
        if not self._authorize():
            return

        if path.startswith("/api/rebuild/"):
            fig = self._figure(path[len("/api/rebuild/"):])
            if not fig:
                return self._error(404, "unknown figure")
            return self._rebuild(fig)

        if path.startswith("/api/history/"):
            fig = self._figure(path[len("/api/history/"):])
            if not fig:
                return self._error(404, "unknown figure")
            body = self._body()
            if not isinstance(body, dict) or not all(
                    isinstance(body.get(k), list) for k in ("undo", "redo")):
                return self._error(400, "expected {undo: [...], redo: [...]}")
            fig.save_history(body["undo"][-HISTORY_MAX:], body["redo"][-HISTORY_MAX:])
            return self._json({"ok": True})

        if path in ("/api/project/duplicate", "/api/project/rename"):
            return self._project_op(path.rsplit("/", 1)[1])

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
        fig.write_outputs(payload["svg"], codegen.generate(spec))
        return self._json(payload)

    def _rebuild(self, fig):
        """Throw away layout edits and regenerate the spec from raw data."""
        from figforge import analyze, spec_builder
        with fig.csv_file() as csv_path:
            arrays, derived = analyze.analyze(csv_path)
        fig.save_arrays(arrays)
        spec = spec_builder.build_spec(derived)
        fig.save_spec(spec)
        fig.save_history([], [])  # the old stacks belong to the discarded edits
        payload = self._payload(fig, spec)
        fig.write_outputs(payload["svg"], codegen.generate(spec))
        return self._json(payload)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            if self.headers.get("X-Body-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw or b"{}")
        except (ValueError, OSError, EOFError):
            return None

    def _project_op(self, op):
        """Projects are just figure folders, so "Save as" is a folder copy and
        rename is a folder move. Never overwrites an existing project."""
        body = self._body()
        if not isinstance(body, dict):
            return self._error(400, "bad request")
        src = self._figure(body.get("from"))
        if not src:
            return self._error(404, "unknown project")
        name = body.get("name") or ""
        if not NAME_RE.match(name):
            return self._error(400, "names may use letters, digits, - and _ only")
        if self.store.project_exists(name):
            return self._error(409, f"a project named '{name}' already exists")
        if op == "duplicate":
            self.store.copy_project(src.name, name)
        else:
            self.store.rename_project(src.name, name)
        return self._json({"name": name, "figures": list_figures(self.store)})

    # ------------------------------------------------------------ accounts
    def _cookie(self, name):
        jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return jar[name].value if name in jar else None

    def _secure(self):
        host = self.headers.get("Host", "")
        return not host.startswith(("127.0.0.1", "localhost"))

    def _set_cookie(self, name, value, max_age):
        flags = "; Secure" if self._secure() else ""
        self._cookies.append(f"{name}={value}; Path=/; Max-Age={max_age}; "
                             f"HttpOnly; SameSite=Lax{flags}")

    def _set_session(self, sess):
        self._set_cookie("ff_at", sess["access_token"], SESSION_MAX_AGE)
        self._set_cookie("ff_rt", sess["refresh_token"], SESSION_MAX_AGE)

    def _clear_session(self):
        self._set_cookie("ff_at", "", 0)
        self._set_cookie("ff_rt", "", 0)

    def _site_url(self):
        """Where Supabase's email links send people back to: this site."""
        proto = self.headers.get("X-Forwarded-Proto") or ("https" if self._secure() else "http")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
        return f"{proto}://{host}/"

    def _current_user(self):
        """The signed-in user from the session cookies, refreshing an expired
        access token when the refresh token still works. None if signed out."""
        at, rt = self._cookie("ff_at"), self._cookie("ff_rt")
        self._access_token = at
        if at:
            try:
                return AUTH.user(at)
            except AuthError as e:
                if e.status >= 500:
                    raise
        if rt:
            try:
                sess = AUTH.refresh(rt)
                self._set_session(sess)
                self._access_token = sess["access_token"]
                return sess["user"]
            except AuthError as e:
                if e.status >= 500:
                    raise
        return None

    def _authorize(self):
        """Pick the storage this request works in. Locally: the one local
        store, no account needed. Hosted: the signed-in user's own space,
        or a 401 that sends the page to its sign-in screen."""
        self.user = None
        self.store = STORE
        if AUTH is None:
            return True
        self.user = self._current_user()
        if not self.user:
            if self._cookie("ff_at") or self._cookie("ff_rt"):
                self._clear_session()
            self._error(401, "sign in required")
            return False
        self.store = STORE.scoped(self.user["id"])
        return True

    def _auth_me(self):
        if AUTH is None:
            return self._json({"mode": "local"})
        user = self._current_user()
        if not user:
            return self._json({"mode": "online", "user": None}, 401)
        return self._json({"mode": "online", "user": public_user(user)})

    def _auth_api(self, op):
        if AUTH is None:
            return self._error(404, "accounts are only available on the hosted editor")
        body = self._body()
        if not isinstance(body, dict):
            return self._error(400, "bad request")

        def text(k):
            return str(body.get(k) or "").strip()

        try:
            if op in ("signup", "login", "recover"):
                email = text("email").lower()
                if not EMAIL_RE.match(email):
                    return self._error(400, "enter a valid email address")
            if op in ("signup", "password"):
                if len(str(body.get("password") or "")) < 8:
                    return self._error(400, "passwords need at least 8 characters")

            if op == "signup":
                sess = AUTH.signup(email, body["password"], text("display_name")[:60],
                                   self._site_url())
                if not sess:
                    return self._json({"confirm": True})
                self._set_session(sess)
                return self._json({"user": public_user(sess["user"])})

            if op == "login":
                sess = AUTH.login(email, str(body.get("password") or ""))
                self._set_session(sess)
                return self._json({"user": public_user(sess["user"])})

            if op == "session":  # tokens from an email confirm / reset link
                at, rt = text("access_token"), text("refresh_token")
                user = AUTH.user(at)  # verified by Supabase before it's trusted
                self._set_session({"access_token": at, "refresh_token": rt})
                return self._json({"user": public_user(user)})

            if op == "recover":
                AUTH.recover(email, self._site_url())
                return self._json({"ok": True})

            if op == "logout":
                if self._cookie("ff_at"):
                    AUTH.logout(self._cookie("ff_at"))
                self._clear_session()
                return self._json({"ok": True})

            if op in ("profile", "password"):
                if not self._current_user():
                    return self._error(401, "sign in required")
                if op == "profile":
                    changes = {"data": {"display_name": text("display_name")[:60]}}
                else:
                    changes = {"password": body["password"]}
                user = AUTH.update(self._access_token, changes)
                return self._json({"user": public_user(user)})
        except AuthError as e:
            code = e.status if e.status in (400, 401, 422, 429) else (401 if e.status == 403 else 502)
            return self._error(code, e.message)
        return self._error(404, "unknown account action")

    # -------------------------------------------------------------- parts
    def _payload(self, fig, spec):
        svg, geom = render.render(spec, fig.load_arrays(), preview=True)
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
