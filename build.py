"""Bootstrap a figure: CSV -> analysis -> spec.json -> figure.svg / figure.py.

    python build.py            rebuild the qcircle figure from scratch
    python build.py --keep     re-render only, keeping any spec edits you made

Run this once, then `python serve.py` to open the editor.
"""

import argparse
import os
import sys

from figforge import analyze, codegen, render, spec_builder
from figforge.project import Figure


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("figure", nargs="?", default="qcircle")
    ap.add_argument("--keep", action="store_true",
                    help="keep the existing spec.json; re-render it only")
    args = ap.parse_args()

    fig = Figure(args.figure)
    if not os.path.isdir(fig.dir):
        sys.exit(f"no such figure folder: {fig.dir}")

    if args.keep and fig.exists():
        spec = fig.load_spec()
        print(f"reusing {fig.spec_path} (rev {spec.get('rev')})")
    else:
        if not os.path.isfile(fig.csv_path):
            sys.exit(f"missing raw data: {fig.csv_path}")
        print(f"analysing {fig.csv_path} ...")
        arrays, derived = analyze.analyze(fig.csv_path)
        fig.save_arrays(arrays)
        print(f"  beta1={derived['b11']:.3f}  beta2={derived['b33']:.3f}  "
              f"QL={derived['QL']:.0f}  Q0={derived['Q0']:.0f}")
        spec = spec_builder.build_spec(derived)
        fig.save_spec(spec)
        print(f"wrote {fig.spec_path}")

    arrays = fig.load_arrays()
    svg, geom = render.render(spec, arrays)
    with open(fig.svg_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"wrote {fig.svg_path}  ({geom['width']:.0f} x {geom['height']:.0f} pt)")

    with open(fig.py_path, "w", encoding="utf-8") as f:
        f.write(codegen.generate(spec))
    print(f"wrote {fig.py_path}")


if __name__ == "__main__":
    main()
