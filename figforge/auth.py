"""Accounts for the hosted editor, on Supabase Auth (stdlib HTTP only).

Only used when FigForge runs with Supabase configured (see project.py);
the local editor has no accounts. The browser never talks to Supabase
directly: the server proxies sign-up / log-in and keeps the resulting
session in HttpOnly cookies, so page scripts can't read the tokens.

Email links (confirm sign-up, reset password) come back to the site as
"#access_token=...&type=signup|recovery" in the URL fragment; the page
posts those tokens to /api/auth/session, which verifies them with Supabase
before turning them into a session.
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class AuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _jwt_exp(token):
    """A token's expiry, read (not verified) from its payload -- only used to
    bound how long a Supabase-verified token stays in the cache."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError):
        return 0.0


class SupabaseAuth:
    CACHE_SECONDS = 300  # re-check a token with Supabase at least this often

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/auth/v1"
        self.key = key
        self._cache = {}  # access token -> (user, valid until)

    # ------------------------------------------------------------ transport
    def _call(self, method, path, body=None, token=None, query=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            msg = (err.get("msg") or err.get("message") or err.get("error_description")
                   or err.get("error") or f"auth request failed ({e.code})")
            raise AuthError(e.code, msg) from None

    # -------------------------------------------------------------- flows
    def signup(self, email, password, display_name, redirect_to):
        """Returns a session if the project doesn't require email
        confirmation, otherwise None (a confirmation email was sent)."""
        res = self._call("POST", "/signup", {
            "email": email, "password": password,
            "data": {"display_name": display_name},
        }, query={"redirect_to": redirect_to})
        # Supabase answers an already-registered email with a user that has
        # no identities, deliberately indistinguishable from success.
        return res if res.get("access_token") else None

    def login(self, email, password):
        return self._call("POST", "/token", {"email": email, "password": password},
                          query={"grant_type": "password"})

    def refresh(self, refresh_token):
        return self._call("POST", "/token", {"refresh_token": refresh_token},
                          query={"grant_type": "refresh_token"})

    def user(self, access_token):
        """The user a token belongs to, verified by Supabase (cached briefly)."""
        hit = self._cache.get(access_token)
        now = time.time()
        if hit and hit[1] > now:
            return hit[0]
        u = self._call("GET", "/user", token=access_token)
        until = min(_jwt_exp(access_token), now + self.CACHE_SECONDS)
        if len(self._cache) > 1000:
            self._cache.clear()
        self._cache[access_token] = (u, until)
        return u

    def update(self, access_token, changes):
        u = self._call("PUT", "/user", changes, token=access_token)
        self._cache.pop(access_token, None)
        return u

    def recover(self, email, redirect_to):
        self._call("POST", "/recover", {"email": email}, query={"redirect_to": redirect_to})

    def logout(self, access_token):
        self._cache.pop(access_token, None)
        try:
            self._call("POST", "/logout", token=access_token)
        except AuthError:
            pass  # already expired / revoked: the cookies get cleared anyway


def public_user(u):
    """What the page is allowed to know about the signed-in user."""
    meta = u.get("user_metadata") or {}
    return {"id": u.get("id"), "email": u.get("email"),
            "display_name": meta.get("display_name") or ""}
