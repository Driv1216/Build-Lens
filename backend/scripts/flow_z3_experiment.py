"""Lab-only differential check of the admissible integer equality fragment; no production Z3.

Uses independently constructed Z3 Int constraints, plus explicit opaque-evaluation controls.
Agreement is bounded to this fragment and seeded input distribution, not whole-program proof.
"""

from __future__ import annotations

import argparse
import random
import time
from collections import Counter
from pathlib import Path

from flow_evaluate import save

from buildlens.domain.flow_trace import Atom, atom_json, decide


def main() -> None:
    import z3  # type: ignore[import-not-found]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rng = random.Random(15124)
    rows = []
    started = time.perf_counter()
    for i in range(1000):
        solver = z3.Solver()
        solver.set(timeout=1000)
        atoms = []
        for _ in range(rng.randrange(1, 10)):
            name = rng.choice(["x", "y", "z"])
            number, polarity = rng.randrange(-3, 4), bool(rng.randrange(2))
            atoms.append(Atom("eq", ("scope", name, (0,)), polarity, ("int", number)))
            predicate = z3.Int(name) == number
            solver.add(predicate if polarity else z3.Not(predicate))
        expected = "refuted_in_fragment" if solver.check() == z3.unsat else "consistent_in_fragment"
        actual = decide(tuple(atoms))
        rows.append(
            {
                "id": i,
                "atoms": [atom_json(a) for a in atoms],
                "z3": expected,
                "normalizer": actual,
                "agrees": expected == actual,
            }
        )
    opaque = decide((Atom("opaque", None, True), Atom("opaque", None, False)))
    branch = decide(
        (Atom("opaque", None, True, branch=("s", 1)), Atom("opaque", None, False, branch=("s", 1)))
    )
    report = {
        "version": "flow-z3-experiment.v1",
        "z3_version": z3.get_version_string(),
        "seed": 15124,
        "n": len(rows),
        "disagreements": sum(not r["agrees"] for r in rows),
        "verdicts": dict(Counter(r["normalizer"] for r in rows)),
        "controls": {"separate_opaque_evaluations": opaque, "same_acyclic_branch": branch},
        "seconds": time.perf_counter() - started,
        "rows": rows,
        "decision": "Keep Z3 lab-only. The existing normalizer handles this fragment; "
        "agreement provides no justification to add a solver to production.",
    }
    save(args.out, report)
    print({k: v for k, v in report.items() if k != "rows"})
    if report["disagreements"] or opaque != "undetermined" or branch != "refuted_in_fragment":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
