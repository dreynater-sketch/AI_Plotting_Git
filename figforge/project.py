"""Figure folder layout and load/save helpers.

A figure is a folder (spec section 6.1):

    figures/qcircle/
        data/vna_sweep.csv     raw measurement
        data/curves.npz        analysed curve arrays (generated)
        spec.json              the semantic layer -- the thing you edit
        figure.svg             last render (generated)
        figure.py              standalone reproducer (generated)
        history.json           editor undo/redo stacks (generated, not versioned)

A figure folder is also a "project" in the editor: Save as copies the folder,
rename moves it.

Where the folders live depends on where FigForge runs:

  * locally (the default): real folders under figures/, as above.
  * hosted (Vercel): a private Supabase Storage bucket, chosen whenever
    SUPABASE_URL and SUPABASE_SECRET_KEY are set. Each account's projects
    live under "users/<user id>/" (see SupabaseStore.scoped and auth.py). A function's own disk
    doesn't survive between requests, so nothing else there would persist.
    The bucket is created and seeded from the figures/ folders bundled with
    the deployment the first time it's missing. figure.svg / figure.py
    aren't stored there -- the editor re-renders the SVG and generates
    figure.py on demand.
"""

import contextlib
import copy
import io
import json
import os
import shutil
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGURES = os.path.join(ROOT, "figures")

# What makes up a project in storage: its spec, its data, and its undo stacks.
# (Seeding copies only the first three; history starts empty.)
PROJECT_FILES = ("spec.json", "data/curves.npz", "data/vna_sweep.csv", "history.json")


class LocalStore:
    """Project folders on this machine's disk."""

    persistent_outputs = True  # figure.svg / figure.py are written alongside

    def _path(self, rel):
        return os.path.join(FIGURES, *rel.split("/"))

    def read(self, rel):
        try:
            with open(self._path(rel), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def write(self, rel, data):
        path = self._path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def has(self, rel):
        return os.path.isfile(self._path(rel))

    def projects(self):
        if not os.path.isdir(FIGURES):
            return []
        return sorted(n for n in os.listdir(FIGURES) if self.has(f"{n}/spec.json"))

    def project_exists(self, name):
        return os.path.exists(self._path(name))

    def copy_project(self, src, dst):
        shutil.copytree(self._path(src), self._path(dst))

    cache_key = "local:"

    def rename_project(self, src, dst):
        os.rename(self._path(src), self._path(dst))


class SupabaseStore:
    """Project folders as "projects/<name>/<file>" objects in a private
    Supabase Storage bucket, over Supabase's plain HTTP API (stdlib only).

    Objects are written with max-age=0 so a spec saved a moment ago is what
    the next request reads, never a cached copy. The bucket is created, as
    private, the first time it's missing, and seeded with the bundled figures
    whenever it holds no projects at all."""

    persistent_outputs = False
    PREFIX = "projects/"

    def __init__(self, url, key, bucket):
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.headers = {"apikey": key}
        # Legacy service_role keys are JWTs and also go in Authorization; the
        # newer sb_secret_ keys are sent as apikey only.
        if key.startswith("eyJ"):
            self.headers["Authorization"] = "Bearer " + key
        self._ready = False

    # ------------------------------------------------------------ transport
    def _call(self, method, path, body=None, headers=None, ok404=False):
        import urllib.error
        import urllib.request
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={**self.headers, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            # Storage reports a missing object as 400 {"statusCode":"404"} as
            # well as a plain 404.
            if ok404 and (e.code == 404 or '"404"' in detail or "not_found" in detail
                          or "not found" in detail.lower()):
                return None
            raise RuntimeError(f"Supabase storage {method} {path}: {e.code} {detail}") from None

    def _obj(self, key):
        from urllib.parse import quote
        return f"/object/{self.bucket}/{quote(key)}"

    def _ensure_bucket(self):
        if self._ready:
            return
        self._ready = True
        if self._call("GET", f"/bucket/{self.bucket}", ok404=True) is None:
            self._call("POST", "/bucket", {"id": self.bucket, "name": self.bucket,
                                           "public": False})

    # ---------------------------------------------------------------- files
    def _key(self, rel):
        return self.PREFIX + rel

    @property
    def cache_key(self):
        return self.PREFIX

    def scoped(self, user_id):
        """The same bucket, seen as one account's own project space."""
        self._ensure_bucket()
        view = copy.copy(self)
        view.PREFIX = f"users/{user_id}/"
        return view

    def read(self, rel):
        self._ensure_bucket()
        return self._call("GET", self._obj(self._key(rel)), ok404=True)

    def write(self, rel, data):
        self._ensure_bucket()
        self._call("POST", self._obj(self._key(rel)), data, {
            "Content-Type": "application/octet-stream",
            "x-upsert": "true",
            "cache-control": "max-age=0",
        })

    def has(self, rel):
        return self.read(rel) is not None

    def _list(self, folder):
        """Direct children of a folder: ([file names], [subfolder names])."""
        self._ensure_bucket()
        items = json.loads(self._call("POST", f"/object/list/{self.bucket}", {
            "prefix": folder, "limit": 1000, "offset": 0,
            "sortBy": {"column": "name", "order": "asc"},
        }))
        files = [i["name"] for i in items if i.get("id")]
        folders = [i["name"] for i in items if not i.get("id")]
        return files, folders

    def _walk(self, folder):
        """Every object key under a folder, recursively."""
        files, folders = self._list(folder)
        out = [f"{folder}/{f}" for f in files if f != ".emptyFolderPlaceholder"]
        for sub in folders:
            out += self._walk(f"{folder}/{sub}")
        return out

    # ------------------------------------------------------------- projects
    def projects(self):
        _, names = self._list(self.PREFIX.rstrip("/"))
        if not names:
            self._seed()
            _, names = self._list(self.PREFIX.rstrip("/"))
        return sorted(n for n in names if self.has(f"{n}/spec.json"))

    def project_exists(self, name):
        return bool(self._walk(self.PREFIX + name))

    def copy_project(self, src, dst):
        for key in self._walk(self.PREFIX + src):
            self._call("POST", "/object/copy", {
                "bucketId": self.bucket, "sourceKey": key,
                "destinationKey": self.PREFIX + dst + key[len(self.PREFIX + src):],
            })

    def rename_project(self, src, dst):
        for key in self._walk(self.PREFIX + src):
            self._call("POST", "/object/move", {
                "bucketId": self.bucket, "sourceKey": key,
                "destinationKey": self.PREFIX + dst + key[len(self.PREFIX + src):],
            })

    def _seed(self):
        """An empty bucket gets the figures bundled with the deployment, so
        the hosted editor opens on something instead of nothing."""
        local = LocalStore()
        for name in local.projects():
            for rel in PROJECT_FILES[:3]:
                data = local.read(f"{name}/{rel}")
                if data is not None:
                    self.write(f"{name}/{rel}", data)


def _make_store():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        return SupabaseStore(url, key, os.environ.get("FIGFORGE_BUCKET", "figforge"))
    if os.environ.get("VERCEL"):
        # Hosted without storage configured: refuse loudly rather than
        # "save" edits to a disk that's wiped after every request.
        raise RuntimeError("FigForge on Vercel needs SUPABASE_URL and "
                           "SUPABASE_SECRET_KEY environment variables")
    return LocalStore()


STORE = _make_store()

# Accounts exist only alongside Supabase storage; the local editor has none.
AUTH = None
if isinstance(STORE, SupabaseStore):
    from figforge.auth import SupabaseAuth
    AUTH = SupabaseAuth(os.environ["SUPABASE_URL"],
                        os.environ.get("SUPABASE_SECRET_KEY")
                        or os.environ["SUPABASE_SERVICE_ROLE_KEY"])

_ARRAY_CACHE = {}


class Figure:
    def __init__(self, name, store=None):
        self.name = name
        self.store = store or STORE
        # Local paths: what build.py and a local figure.py work with.
        self.dir = os.path.join(FIGURES, name)
        self.spec_path = os.path.join(self.dir, "spec.json")
        self.svg_path = os.path.join(self.dir, "figure.svg")
        self.py_path = os.path.join(self.dir, "figure.py")
        self.history_path = os.path.join(self.dir, "history.json")
        self.npz_path = os.path.join(self.dir, "data", "curves.npz")
        self.csv_path = os.path.join(self.dir, "data", "vna_sweep.csv")

    def _rel(self, file):
        return f"{self.name}/{file}"

    # ---------------------------------------------------------------- spec
    def load_spec(self):
        return json.loads(self.store.read(self._rel("spec.json")).decode("utf-8"))

    def save_spec(self, spec):
        self.store.write(self._rel("spec.json"),
                    json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8"))

    def write_outputs(self, svg, code):
        """figure.svg / figure.py next to the spec -- locally only. On Vercel
        the editor renders and generates these on demand instead."""
        if not self.store.persistent_outputs:
            return
        self.store.write(self._rel("figure.svg"), svg.encode("utf-8"))
        self.store.write(self._rel("figure.py"), code.encode("utf-8"))

    # ------------------------------------------------------------- history
    def load_history(self):
        """The editor's undo/redo stacks, so both survive closing the browser.
        A missing or unreadable file just means no history."""
        try:
            h = json.loads(self.store.read(self._rel("history.json")) or b"null")
        except ValueError:
            h = None
        if isinstance(h, list):  # the first format: an undo stack only
            h = {"undo": h, "redo": []}
        if not isinstance(h, dict):
            h = {}
        return {k: h.get(k) if isinstance(h.get(k), list) else []
                for k in ("undo", "redo")}

    def save_history(self, undo, redo):
        self.store.write(self._rel("history.json"),
                    json.dumps({"undo": undo, "redo": redo},
                               ensure_ascii=False).encode("utf-8"))

    # ---------------------------------------------------------------- data
    def load_arrays(self):
        """Cached -- the editor re-renders constantly and the arrays only
        change when the figure is rebuilt from raw data. Locally the cache
        is keyed on the npz's mtime; on Vercel, on the project name (a
        rebuild regenerates identical arrays from the same CSV anyway)."""
        stamp = os.path.getmtime(self.npz_path) if self.store.persistent_outputs else 0
        key = self.store.cache_key + self.name
        hit = _ARRAY_CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        with np.load(io.BytesIO(self.store.read(self._rel("data/curves.npz")))) as z:
            arrays = {k: z[k] for k in z.files}
        _ARRAY_CACHE[key] = (stamp, arrays)
        return arrays

    def save_arrays(self, arrays):
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        self.store.write(self._rel("data/curves.npz"), buf.getvalue())
        _ARRAY_CACHE.pop(self.store.cache_key + self.name, None)

    @contextlib.contextmanager
    def csv_file(self):
        """A real path to the raw CSV, for analyze() -- a temp copy on Vercel."""
        if self.store.persistent_outputs:
            yield self.csv_path
            return
        data = self.store.read(self._rel("data/vna_sweep.csv"))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "vna_sweep.csv")
            with open(path, "wb") as f:
                f.write(data)
            yield path

    def exists(self):
        return self.store.has(self._rel("spec.json"))


def list_figures(store=None):
    return (store or STORE).projects()
