"""Figure folder layout and load/save helpers.

A figure is a folder (spec section 6.1):

    figures/qcircle/
        data/vna_sweep.csv     raw measurement
        data/curves.npz        analysed curve arrays (generated)
        spec.json              the semantic layer -- the thing you edit
        figure.svg             last render (generated)
        figure.py              standalone reproducer (generated)
"""

import json
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGURES = os.path.join(ROOT, "figures")

_ARRAY_CACHE = {}


class Figure:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(FIGURES, name)
        self.spec_path = os.path.join(self.dir, "spec.json")
        self.svg_path = os.path.join(self.dir, "figure.svg")
        self.py_path = os.path.join(self.dir, "figure.py")
        self.npz_path = os.path.join(self.dir, "data", "curves.npz")
        self.csv_path = os.path.join(self.dir, "data", "vna_sweep.csv")

    # ---------------------------------------------------------------- spec
    def load_spec(self):
        with open(self.spec_path, encoding="utf-8") as f:
            return json.load(f)

    def save_spec(self, spec):
        tmp = self.spec_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.spec_path)

    # ---------------------------------------------------------------- data
    def load_arrays(self):
        """Cached on the npz's mtime -- the editor re-renders constantly and
        the arrays only change when the figure is rebuilt from raw data."""
        stamp = os.path.getmtime(self.npz_path)
        hit = _ARRAY_CACHE.get(self.npz_path)
        if hit and hit[0] == stamp:
            return hit[1]
        with np.load(self.npz_path) as z:
            arrays = {k: z[k] for k in z.files}
        _ARRAY_CACHE[self.npz_path] = (stamp, arrays)
        return arrays

    def save_arrays(self, arrays):
        os.makedirs(os.path.dirname(self.npz_path), exist_ok=True)
        np.savez_compressed(self.npz_path, **arrays)
        _ARRAY_CACHE.pop(self.npz_path, None)

    def exists(self):
        return os.path.isfile(self.spec_path)


def list_figures():
    if not os.path.isdir(FIGURES):
        return []
    return sorted(n for n in os.listdir(FIGURES)
                  if Figure(n).exists())
