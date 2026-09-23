"""FigForge on Vercel: the local editor's server, run as one Python function.

vercel.json rewrites every URL -- the editor page, its JS/CSS, and /api/* --
to this function, and the same Handler that `python serve.py` runs answers
all of it. The differences from local are all in figforge/project.py:
projects are kept in a private Supabase Storage bucket (SUPABASE_URL and
SUPABASE_SECRET_KEY, set in the Vercel project's environment variables),
since a function's disk doesn't persist.

Access control is Vercel's Deployment Protection on the project (only
signed-in members of the Vercel team can reach any URL); nothing here
authenticates on its own.
"""

import os
import sys

# matplotlib wants a writable config/cache dir; only /tmp is writable here.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from figforge.server import Handler  # noqa: E402


class handler(Handler):
    pass
