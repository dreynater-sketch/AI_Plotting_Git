"""Copy a project from this computer (figures/<name>) into the admin's
online FigForge account, so it shows up in the project list on the site.

    python tools/to_account.py                 # list the admin's online projects
    python tools/to_account.py <name> [<new>]  # copy figures/<name> online (as <new>)

Reads SUPABASE_URL / SUPABASE_SECRET_KEY from the git-ignored .env (never
printed). An existing online project of the same name is never overwritten.
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from figforge.project import LocalStore, SupabaseStore  # noqa: E402


def admin_store():
    env = {}
    for line in open(".env", encoding="utf-8"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.strip().strip('"')
    url, key = env["SUPABASE_URL"], env["SUPABASE_SECRET_KEY"]
    req = urllib.request.Request(f"{url}/auth/v1/admin/users?per_page=1000",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}"})
    admins = [u for u in json.load(urllib.request.urlopen(req))["users"]
              if (u.get("app_metadata") or {}).get("figforge_admin")]
    if len(admins) != 1:
        sys.exit(f"expected exactly one admin account, found {len(admins)}")
    return SupabaseStore(url, key, "figforge").scoped(admins[0]["id"])


def main():
    online = admin_store()
    if len(sys.argv) < 2:
        print("\n".join(online.projects()))
        return
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else src
    local = LocalStore()
    if not local.project_exists(src):
        sys.exit(f"no local project figures/{src}")
    if online.project_exists(dst):
        sys.exit(f"the account already has a project named {dst!r} -- pick another name")
    base = os.path.join("figures", src)
    n = 0
    for folder, _, files in os.walk(base):
        for f in files:
            rel = os.path.relpath(os.path.join(folder, f), base).replace(os.sep, "/")
            with open(os.path.join(folder, f), "rb") as fh:
                online.write(f"{dst}/{rel}", fh.read())
            n += 1
    print(f"copied {n} files: {src} -> your account as {dst!r}")


if __name__ == "__main__":
    main()
