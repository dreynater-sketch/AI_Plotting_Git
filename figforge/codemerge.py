"""Figure -> saved code: carry changes made in the figure editor into the
user's hand-edited figure.py.

A three-way merge by lines, like a version-control merge. `base` is the code
FigForge generated when the user last saved their version, `mine` is their
version, `theirs` the code generated from the figure as it is now. Lines only
the figure changed take the figure's version, lines only the user changed
keep the user's, and where both changed the same lines differently the
user's lines win and the place is reported. Plain text only: nothing runs.
"""

import difflib


def _hunks(base, other):
    """[(lo, hi, lines)]: base[lo:hi] becomes lines in `other`."""
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _overlap(a1, a2, b1, b2):
    if a1 == a2 and b1 == b2:          # two insertions at one place
        return a1 == b1
    if a1 == a2:                       # an insertion inside the other change
        return b1 < a1 < b2
    if b1 == b2:
        return a1 < b1 < a2
    return a1 < b2 and b1 < a2


def _apply(base, lo, hi, hunks):
    """base[lo:hi] with this side's hunks (all inside lo..hi) applied."""
    out, i = [], lo
    for h1, h2, lines in hunks:
        out += base[i:h1] + lines
        i = h2
    return out + base[i:hi]


def merge3(base, mine, theirs):
    """-> (merged text, conflicts). conflicts: [{"line", "lines"}] -- where
    in the merged text the user's lines were kept over the figure's (1-based
    first line, number of lines)."""
    if mine == base:
        return theirs, []
    if theirs == base or theirs == mine:
        return mine, []
    b, m, t = (s.splitlines(True) for s in (base, mine, theirs))
    # A missing newline at the end would glue two lines together.
    for side in (b, m, t):
        if side and not side[-1].endswith("\n"):
            side[-1] += "\n"
    tagged = sorted([(lo, hi, lines, "m") for lo, hi, lines in _hunks(b, m)] +
                    [(lo, hi, lines, "t") for lo, hi, lines in _hunks(b, t)],
                    key=lambda h: (h[0], h[1]))
    groups = []
    for h in tagged:
        g = groups[-1] if groups else None
        if g and _overlap(g["lo"], g["hi"], h[0], h[1]):
            g["lo"], g["hi"] = min(g["lo"], h[0]), max(g["hi"], h[1])
            g["hunks"].append(h)
        else:
            groups.append({"lo": h[0], "hi": h[1], "hunks": [h]})

    out, conflicts, i = [], [], 0
    for g in groups:
        out += b[i:g["lo"]]
        sides = {s: [(lo, hi, lines) for lo, hi, lines, who in g["hunks"] if who == s] for s in "mt"}
        mine_part = _apply(b, g["lo"], g["hi"], sides["m"])
        theirs_part = _apply(b, g["lo"], g["hi"], sides["t"])
        if not sides["m"]:
            out += theirs_part
        elif not sides["t"] or mine_part == theirs_part:
            out += mine_part
        else:
            conflicts.append({"line": len(out) + 1, "lines": len(mine_part)})
            out += mine_part
        i = g["hi"]
    out += b[i:]
    text = "".join(out)
    if not mine.endswith("\n") and text.endswith("\n"):
        text = text[:-1]
    return text, conflicts
