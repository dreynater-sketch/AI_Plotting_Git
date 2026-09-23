"""Local-only editor server. Stdlib http.server, no dependencies, no network.

    GET  /                     the editor
    GET  /api/figures          names of available figures
    GET  /api/figure/<name>    {spec, svg, geometry}
    POST /api/figure/<name>    {spec} -> re-render -> {spec, svg, geometry}
    GET  /api/code/<name>      standalone figure.py (text/plain)
    GET  /api/png/<name>       rendered PNG at the spec's export dpi
    GET  /api/code/<name>?edited=1   the user's hand-edited figure.py instead
    GET  /api/script/<name>    {generated, custom: {code, base_rev, saved_at} | null, rev}
    POST /api/script/<name>    {code} -> save the hand-edited figure.py and apply
                               what it can back onto the figure (codesync.py)
    POST /api/script/<name>/reset   drop the hand-edited version
    GET  /api/npz/<name>       data/curves.npz, for running figure.py in the browser
    GET  /api/history/<name>   saved undo/redo stacks {undo: [spec...], redo: [...]}
    POST /api/history/<name>   {undo, redo} -> persist both stacks
    POST /api/project/duplicate  {from, name} -> copy a project ("Save as")
    POST /api/project/rename     {from, name} -> rename a project folder
    POST /api/csv/inspect        {csv} -> the file's columns, for picking x / y
    GET  /api/assistant/tools    editing operations as Claude API tool definitions (ops.py)
    GET  /api/describe/<name>    the compact figure summary an assistant reads first
    POST /api/ops/<name>         {calls: [{name, input}]} -> apply them; one result per call
    POST /api/project/from-csv   {name, csv, filename, x, ys, plot} -> new project

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
    GET  /api/admin/users      (admins) everyone with an account or an invite
    POST /api/admin/invite     (admins) {email} -> Supabase emails an invite link

Sign-up is invite-only unless FIGFORGE_OPEN_SIGNUP=1 is set.

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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from figforge import codegen, codesync, csvimport, ops, render
from figforge.auth import AuthError, is_admin, public_user
from figforge.project import AUTH, ROOT, STORE, Figure, list_figures

WEB = os.path.join(ROOT, "web")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HISTORY_MAX = 100  # per stack; matches the editor's HISTORY_MAX
OPEN_SIGNUP = os.environ.get("FIGFORGE_OPEN_SIGNUP") == "1"
SCRIPT_MAX = 512 * 1024  # a hand-edited figure.py; the generated one is ~6 KB
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
        if path == "/api/admin/users":
            return self._admin_users()
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
            if "edited=1" in urlparse(self.path).query:
                custom = fig.load_script()
                if not custom:
                    return self._error(404, "no edited version saved")
                return self._send(200, custom["code"], "text/plain; charset=utf-8")
            return self._send(200, codegen.generate(fig.load_spec()),
                              "text/plain; charset=utf-8")

        if path.startswith("/api/script/"):
            fig = self._figure(path[len("/api/script/"):])
            if not fig:
                return self._error(404, "unknown figure")
            spec = fig.load_spec()
            return self._json({"generated": codegen.generate(spec),
                               "custom": fig.load_script(), "rev": spec.get("rev")})

        if path.startswith("/api/npz/"):
            fig = self._figure(path[len("/api/npz/"):])
            if not fig:
                return self._error(404, "unknown figure")
            return self._send(200, fig.npz_bytes(), "application/octet-stream")

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

        if path == "/api/assistant/tools":
            return self._json({"model": "claude-opus-5", "tools": ops.TOOLS})

        if path.startswith("/api/describe/"):
            fig = self._figure(path[len("/api/describe/"):])
            if not fig:
                return self._error(404, "unknown figure")
            return self._json(ops.describe(fig.load_spec(), fig.load_arrays()))

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
                # NaN (a gap in imported data) isn't valid JSON: send null.
                clean = lambda vs: [None if v != v else float(v) for v in vs]
                return self._json({
                    "id": series_id, "panel": p["id"], "label": s.get("label", ""),
                    "x": clean(x), "y": clean(y),
                })
        return self._error(404, "unknown series")

    # --------------------------------------------------------------- POST
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        self._cookies = []
        if path.startswith("/api/auth/"):
            return self._auth_api(path[len("/api/auth/"):])
        if path == "/api/admin/invite":
            return self._admin_invite()
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

        if path == "/api/csv/inspect":
            body = self._body()
            if not isinstance(body, dict) or not isinstance(body.get("csv"), str):
                return self._error(400, "expected {csv}")
            try:
                return self._json(csvimport.inspect(body["csv"]))
            except csvimport.CSVError as e:
                return self._error(400, str(e))

        if path == "/api/project/from-csv":
            return self._project_from_csv()

        if path.startswith("/api/ops/"):
            fig = self._figure(path[len("/api/ops/"):])
            if not fig:
                return self._error(404, "unknown figure")
            body = self._body()
            calls = body.get("calls") if isinstance(body, dict) else None
            if not isinstance(calls, list) or len(calls) > 50:
                return self._error(400, "expected {calls: [{name, input}, ...]} (at most 50)")
            return self._apply_ops(fig, calls)

        if path.startswith("/api/script/"):
            rest = path[len("/api/script/"):]
            reset = rest.endswith("/reset")
            fig = self._figure(rest[:-len("/reset")] if reset else rest)
            if not fig:
                return self._error(404, "unknown figure")
            if reset:
                fig.clear_script()
                return self._json({"ok": True})
            body = self._body()
            code = body.get("code") if isinstance(body, dict) else None
            if not isinstance(code, str):
                return self._error(400, "expected {code, base_rev}")
            if len(code.encode("utf-8")) > SCRIPT_MAX:
                return self._error(413, "that script is over 512 KB")
            return self._save_script(fig, code)

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

    def _project_from_csv(self):
        body = self._body()
        if not isinstance(body, dict) or not isinstance(body.get("csv"), str):
            return self._error(400, "expected {name, csv, x, ys}")
        name = str(body.get("name") or "")
        if not NAME_RE.match(name):
            return self._error(400, "names may use letters, digits, - and _ only")
        if self.store.project_exists(name):
            return self._error(409, f"a project named '{name}' already exists")
        try:
            spec, arrays = csvimport.build(name, body["csv"], body.get("x"), body.get("ys") or [],
                                           body.get("plot", "line"), str(body.get("filename") or ""))
        except csvimport.CSVError as e:
            return self._error(400, str(e))
        fig = Figure(name, self.store)
        fig.save_arrays(arrays)
        try:
            payload = self._payload(fig, spec)
        except Exception as e:
            return self._error(422, f"couldn't draw that data: {type(e).__name__}: {e}")
        fig.save_source_csv(body["csv"])
        fig.save_spec(spec)
        fig.write_outputs(payload["svg"], codegen.generate(spec))
        return self._json({"name": name, "figures": list_figures(self.store)})

    def _rebuild(self, fig):
        """Throw away layout edits and regenerate the spec from raw data:
        the Q-circle analysis, or the column choices a CSV project was made with."""
        source = fig.load_spec().get("source") or {}
        if source.get("kind") == "csv":
            text = fig.load_source_csv()
            if text is None:
                return self._error(404, "this project's source CSV is missing")
            spec, arrays = csvimport.build(fig.name, text, source["x"], source["ys"],
                                           source.get("plot", "line"), source.get("filename", ""))
            fig.save_arrays(arrays)
            return self._finish_rebuild(fig, spec)
        from figforge import analyze, spec_builder
        with fig.csv_file() as csv_path:
            arrays, derived = analyze.analyze(csv_path)
        fig.save_arrays(arrays)
        return self._finish_rebuild(fig, spec_builder.build_spec(derived))

    def _finish_rebuild(self, fig, spec):
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

    def _apply_ops(self, fig, calls):
        """Editing operations (ops.py) -- what a future assistant's tool calls
        run through. Saved only if the result renders; the old spec goes on
        the undo stack so the whole batch undoes in one step."""
        spec = fig.load_spec()
        arrays = fig.load_arrays()
        new_spec, results, changed = ops.apply(spec, calls, arrays)
        payload = None
        if changed:
            new_spec["rev"] = int(spec.get("rev", 0)) + 1
            try:
                payload = self._payload(fig, new_spec)
            except Exception as e:
                return self._json({"changed": False, "rev": spec.get("rev"), "results": results,
                                   "error": f"the figure couldn't be drawn after these edits "
                                            f"({type(e).__name__}: {e}); nothing was saved"}, 422)
            fig.save_spec(new_spec)
            fig.write_outputs(payload["svg"], codegen.generate(new_spec))
            h = fig.load_history()
            fig.save_history((h["undo"] + [spec])[-HISTORY_MAX:], [])
        out = {"changed": changed, "rev": (new_spec if changed else spec).get("rev"), "results": results}
        if payload:
            out.update(payload)
        return self._json(out)

    def _save_script(self, fig, code):
        """Keep the user's code as written, then fold what it can into the
        figure. The spec only changes if the result renders; the old spec
        goes onto the undo stack so the figure editor can undo the sync."""
        saved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        spec = fig.load_spec()
        sync = {"changed": [], "added": [], "removed": [], "skipped": [], "error": None}
        new_spec = None
        try:
            new_spec, report = codesync.apply(code, spec, set(fig.load_arrays()))
            sync.update(report)
        except SyntaxError as e:
            sync["error"] = f"Python syntax error on line {e.lineno}: {e.msg} -- saved, but not applied"
        except (RecursionError, MemoryError):
            sync["error"] = "that code is too deeply nested to read -- saved, but not applied"

        if new_spec is not None and (sync["changed"] or sync["added"] or sync["removed"]):
            new_spec["rev"] = int(spec.get("rev", 0)) + 1
            try:
                payload = self._payload(fig, new_spec)
            except Exception as e:  # a value matplotlib rejects: keep the figure as it was
                sync["error"] = (f"the figure couldn't be drawn with these changes "
                                 f"({type(e).__name__}: {e}) -- saved, but not applied")
                sync["changed"] = sync["added"] = sync["removed"] = []
            else:
                fig.save_spec(new_spec)
                fig.write_outputs(payload["svg"], codegen.generate(new_spec))
                h = fig.load_history()
                fig.save_history((h["undo"] + [spec])[-HISTORY_MAX:], [])
                spec = new_spec
        fig.save_script(code, spec.get("rev"), saved_at)
        return self._json({"ok": True, "saved_at": saved_at, "rev": spec.get("rev"),
                           "generated": codegen.generate(spec), "sync": sync})

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
            return self._json({"mode": "online", "user": None, "invite_only": not OPEN_SIGNUP}, 401)
        return self._json({"mode": "online", "user": public_user(user), "invite_only": not OPEN_SIGNUP})

    def _require_admin(self):
        if AUTH is None:
            self._error(404, "accounts are only available on the hosted editor")
            return None
        user = self._current_user()
        if not user:
            self._error(401, "sign in required")
            return None
        if not is_admin(user):
            self._error(403, "only the site admin can manage invites")
            return None
        return user

    def _admin_users(self):
        if not self._require_admin():
            return
        try:
            users = AUTH.list_users()
        except AuthError as e:
            return self._error(502, e.message)
        rows = [{"email": u.get("email"),
                 "display_name": (u.get("user_metadata") or {}).get("display_name") or "",
                 "status": "active" if u.get("last_sign_in_at") else
                           ("invited" if u.get("invited_at") else "unconfirmed"),
                 "invited_at": u.get("invited_at"), "last_sign_in_at": u.get("last_sign_in_at"),
                 "admin": is_admin(u)} for u in users]
        rows.sort(key=lambda r: (r["status"] != "invited", (r["email"] or "").lower()))
        return self._json({"users": rows})

    def _admin_invite(self):
        self._cookies = []
        if not self._require_admin():
            return
        body = self._body()
        email = str((body or {}).get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            return self._error(400, "enter a valid email address")
        try:
            AUTH.invite(email, self._site_url())
        except AuthError as e:
            if "already" in e.message.lower() or e.status == 422:
                return self._error(409, f"{email} already has an account")
            code = 429 if e.status == 429 else 502
            return self._error(code, e.message)
        return self._json({"ok": True, "email": email})

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
                if not OPEN_SIGNUP:
                    return self._error(403, "FigForge is invite-only -- ask the site owner for an invite")
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
