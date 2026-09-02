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
        with np.load(self.npz_path) as z:
            return {k: z[k] for k in z.files}

    def save_arrays(self, arrays):
        os.makedirs(os.path.dirname(self.npz_path), exist_ok=True)
        np.savez_compressed(self.npz_path, **arrays)

    def exists(self):
        return os.path.isfile(self.spec_path)


def list_figures():
    if not os.path.isdir(FIGURES):
        return []
    return sorted(n for n in os.listdir(FIGURES)
                  if Figure(n).exists())
