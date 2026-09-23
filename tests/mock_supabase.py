"""In-memory stand-in for Supabase Storage + Auth HTTP APIs (the subset FigForge
uses). Storage: apikey check, private buckets, x-upsert, missing objects as
400 {"statusCode":"404"}. Auth: email/password sign-up with confirmation,
password grant, refresh-token rotation, /user, profile/password update,
recovery, logout. Emails go to an in-memory mailbox (GET /_mailbox) holding
the final redirect URL Supabase would send the browser to."""
import json, secrets, sys, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs, urlencode

KEY = "sb_secret_test"
buckets, objs = {}, {}
users = {}          # email -> {id, email, password, confirmed, meta}
access = {}         # token -> (email, exp)
refresh = {}        # token -> email
mailbox = []


def session_for(email):
    at, rt = "at-" + secrets.token_hex(8), "rt-" + secrets.token_hex(8)
    access[at] = (email, time.time() + 3600)
    refresh[rt] = email
    return {"access_token": at, "refresh_token": rt, "expires_in": 3600,
            "token_type": "bearer", "user": user_json(users[email])}


def user_json(u):
    return {"id": u["id"], "email": u["email"], "user_metadata": u["meta"],
            "app_metadata": u.get("app", {}), "identities": [{"id": u["id"]}],
            "invited_at": u.get("invited_at"), "last_sign_in_at": u.get("last_sign_in_at")}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _out(self, code, obj=None, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0)); return self.rfile.read(n)

    def _auth(self):
        if self.headers.get("apikey") != KEY:
            self._out(401, {"message": "Invalid API key"}); return False
        return True

    def _bearer_user(self):
        tok = (self.headers.get("Authorization") or "")[7:]
        hit = access.get(tok)
        if not hit or hit[1] < time.time():
            return None
        return users.get(hit[0])

    # ------------------------------------------------------------ routing
    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_PUT(self): self._route("PUT")
    def do_DELETE(self): self._route("DELETE")

    def _route(self, method):
        u = urlparse(self.path); p = unquote(u.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if p == "/_mailbox": return self._out(200, mailbox)
        if p == "/_expire":  # every access token expires now
            for t in list(access): access[t] = (access[t][0], 0)
            return self._out(200, {"ok": True})
        if p == "/_revoke":  # all sessions gone (signed out everywhere)
            access.clear(); refresh.clear(); return self._out(200, {"ok": True})
        if p == "/_debug": return self._out(200, {"objects": sorted(k for _, k in objs)})
        if p == "/_make_admin":  # what setting app_metadata with the service key does
            users[q["email"]].setdefault("app", {})["figforge_admin"] = True
            return self._out(200, {"ok": True})
        if p == "/_confirm":     # a confirmed account, no email round trip
            users[q["email"]]["confirmed"] = True
            return self._out(200, {"ok": True})
        if not self._auth(): return
        body = self._body() if method in ("POST", "PUT", "DELETE") else b""
        if p.startswith("/auth/v1"): return self._auth_route(method, p[8:], q, body)
        if p.startswith("/storage/v1"): return self._storage(method, p[11:], body)
        self._out(404, {})

    # --------------------------------------------------------------- auth
    def _auth_route(self, m, p, q, body):
        d = json.loads(body or b"{}")
        if m == "POST" and p == "/signup":
            email = d["email"]
            if email in users:  # Supabase: looks like success, no identities
                return self._out(200, {"id": str(uuid.uuid4()), "email": email, "identities": []})
            users[email] = {"id": str(uuid.uuid4()), "email": email, "password": d["password"],
                            "confirmed": False, "meta": d.get("data") or {}}
            s = session_for(email)
            mailbox.append({"to": email, "type": "signup", "link": q["redirect_to"] + "#" + urlencode(
                {"access_token": s["access_token"], "refresh_token": s["refresh_token"], "type": "signup"})})
            users[email]["pending"] = s["access_token"]
            return self._out(200, user_json(users[email]))
        if m == "POST" and p == "/token" and q.get("grant_type") == "password":
            u = users.get(d.get("email"))
            if not u or u["password"] != d.get("password"):
                return self._out(400, {"error": "invalid_grant", "error_description": "Invalid login credentials"})
            if not u["confirmed"]:
                return self._out(400, {"error": "invalid_grant", "error_description": "Email not confirmed"})
            u["last_sign_in_at"] = "2026-09-23T12:00:00Z"
            return self._out(200, session_for(u["email"]))
        if m == "POST" and p == "/token" and q.get("grant_type") == "refresh_token":
            email = refresh.pop(d.get("refresh_token"), None)
            if not email:
                return self._out(400, {"error": "invalid_grant", "error_description": "Invalid Refresh Token: Refresh Token Not Found"})
            return self._out(200, session_for(email))
        if m == "GET" and p == "/user":
            u = self._bearer_user()
            if not u: return self._out(403, {"code": 403, "error_code": "bad_jwt", "msg": "invalid JWT: token is expired"})
            if u.get("pending") == (self.headers.get("Authorization") or "")[7:]:
                u["confirmed"] = True  # following the email link confirms the address
            return self._out(200, user_json(u))
        if m == "PUT" and p == "/user":
            u = self._bearer_user()
            if not u: return self._out(401, {"msg": "invalid JWT"})
            if "password" in d:
                if len(d["password"]) < 6: return self._out(422, {"msg": "Password should be at least 6 characters."})
                u["password"] = d["password"]
            if "data" in d: u["meta"].update(d["data"])
            return self._out(200, user_json(u))
        if m == "POST" and p == "/invite":
            email = d["email"]
            if email in users:
                return self._out(422, {"code": 422, "error_code": "email_exists",
                                       "msg": "A user with this email address has already been registered"})
            users[email] = {"id": str(uuid.uuid4()), "email": email, "password": secrets.token_hex(8),
                            "confirmed": False, "meta": {}, "invited_at": "2026-09-23T12:00:00Z"}
            s = session_for(email)
            users[email]["pending"] = s["access_token"]
            mailbox.append({"to": email, "type": "invite", "link": q["redirect_to"] + "#" + urlencode(
                {"access_token": s["access_token"], "refresh_token": s["refresh_token"], "type": "invite"})})
            return self._out(200, user_json(users[email]))
        if m == "GET" and p == "/admin/users":
            return self._out(200, {"users": [user_json(u) for u in users.values()]})
        if m == "POST" and p == "/recover":
            u = users.get(d.get("email"))
            if u:
                s = session_for(u["email"])
                mailbox.append({"to": u["email"], "type": "recovery", "link": q["redirect_to"] + "#" + urlencode(
                    {"access_token": s["access_token"], "refresh_token": s["refresh_token"], "type": "recovery"})})
            return self._out(200, {})
        if m == "POST" and p == "/logout":
            u = self._bearer_user()
            if u:
                for t in [t for t, e in refresh.items() if e == u["email"]]: del refresh[t]
                for t in [t for t, (e, _) in access.items() if e == u["email"]]: del access[t]
            return self._out(204, raw=b"")
        self._out(404, {"msg": f"mock: no {m} {p}"})

    # ------------------------------------------------------------ storage
    def _storage(self, m, p, body):
        if m == "GET" and p.startswith("/bucket/"):
            b = p[8:]
            return self._out(200, buckets[b]) if b in buckets else self._out(400, {"statusCode": "404", "error": "Bucket not found"})
        if m == "POST" and p == "/bucket":
            d = json.loads(body); buckets[d["id"]] = d; return self._out(200, {"name": d["id"]})
        if m == "POST" and p.startswith("/object/list/"):
            b = p[len("/object/list/"):]; d = json.loads(body); pre = d["prefix"].strip("/")
            names = {}
            for (bb, k) in objs:
                if bb != b or not k.startswith(pre + "/"): continue
                rest = k[len(pre) + 1:]
                if "/" in rest: names.setdefault(rest.split("/")[0], None)
                else: names[rest] = "id-" + rest
            return self._out(200, [{"name": n, "id": i} for n, i in sorted(names.items())])
        if m == "POST" and p in ("/object/copy", "/object/move"):
            d = json.loads(body); src = (d["bucketId"], d["sourceKey"]); dst = (d["bucketId"], d["destinationKey"])
            if src not in objs: return self._out(400, {"statusCode": "404", "error": "not_found"})
            if dst in objs: return self._out(400, {"statusCode": "409", "error": "Duplicate"})
            objs[dst] = objs[src]
            if p.endswith("move"): del objs[src]
            return self._out(200, {"message": "ok"})
        if m == "DELETE" and p.startswith("/object/") and "/" not in p[8:]:
            b = p[8:]
            for key in json.loads(body).get("prefixes", []):
                objs.pop((b, key), None)
            return self._out(200, [])
        if p.startswith("/object/"):
            b, key = p[8:].split("/", 1)
            if m == "GET":
                if (b, key) in objs: return self._out(200, raw=objs[(b, key)])
                return self._out(400, {"statusCode": "404", "error": "not_found", "message": "Object not found"})
            if m == "POST":
                if b not in buckets: return self._out(400, {"statusCode": "404", "error": "Bucket not found"})
                if (b, key) in objs and self.headers.get("x-upsert") != "true":
                    return self._out(400, {"statusCode": "409", "error": "Duplicate"})
                objs[(b, key)] = body; return self._out(200, {"Key": f"{b}/{key}"})
        self._out(404, {})


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
