#!/usr/bin/env python3
"""Runner unique des tests de laser_pcb.

Lance les suites (unités par module + intégration bout-en-bout), agrège les
résultats et retourne un code de sortie global. Pensé pour le debug quotidien :

    python tests/run_all.py                   # tout
    python tests/run_all.py --only unit       # une seule suite
    python tests/run_all.py -k centerline     # filtre pytest -k
    python tests/run_all.py --cov             # avec couverture
    python tests/run_all.py -v                # verbeux
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SUITES = {
    "unit": {
        "args": ["tests/unit/"],
        "cov": ["--cov=laserpcb", "--cov-report=term-missing"],
        "description": "Unités par module (geometry, odb, netlist, toolpath, gcode)",
    },
    "integration": {
        "args": ["tests/integration/"],
        "cov": ["--cov=laserpcb", "--cov-append", "--cov-report=term-missing"],
        "description": "Bout-en-bout : ODB++ -> shapely -> axe médian -> G-code -> relecture",
    },
}

NO_TESTS_CODE = 5  # pytest : aucun test collecté


def run_suite(name: str, opts: argparse.Namespace) -> dict:
    suite = SUITES[name]
    cmd = [sys.executable, "-X", "utf8", "-m", "pytest", *suite["args"]]
    if opts.verbose:
        cmd.append("-v")
    if opts.keyword:
        cmd += ["-k", opts.keyword]
    if opts.cov:
        cmd += suite["cov"]
    cmd += opts.pytest_args

    print(f"\n{'=' * 70}\nSUITE [{name}] — {suite['description']}\n{'=' * 70}")
    start = time.time()
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    duration = time.time() - start
    ok = proc.returncode in (0, NO_TESTS_CODE)
    return {"name": name, "ok": ok, "code": proc.returncode, "duration": duration}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lance les suites de test de laser_pcb.")
    parser.add_argument("--only", choices=sorted(SUITES), action="append",
                        help="ne lancer que cette suite (répétable)")
    parser.add_argument("-k", dest="keyword", help="filtre pytest -k")
    parser.add_argument("--cov", action="store_true", help="mesurer la couverture")
    parser.add_argument("-v", "--verbose", action="store_true", help="sortie verbeuse")
    parser.add_argument("pytest_args", nargs="*", help="arguments pytest supplémentaires")
    opts = parser.parse_args(argv)

    names = opts.only or list(SUITES)
    results = [run_suite(name, opts) for name in names]

    print(f"\n{'=' * 70}\nRÉSUMÉ\n{'=' * 70}")
    all_ok = True
    for r in results:
        status = "OK    " if r["ok"] else "ÉCHEC "
        note = " (aucun test collecté)" if (r["ok"] and r["code"] != 0) else ""
        print(f"  [{status}] {r['name']:<12} {r['duration']:6.1f}s{note}")
        all_ok = all_ok and r["ok"]
    print(f"{'=' * 70}\n{'TOUT VERT' if all_ok else 'DES TESTS ÉCHOUENT — voir ci-dessus'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
