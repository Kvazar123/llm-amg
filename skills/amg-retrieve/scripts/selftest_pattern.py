#!/usr/bin/env python3
"""
selftest_pattern.py — proves Stage 17 pattern nodes work and the task-5 guard metrics
fire. Builds a labeled demo (an anti_pattern with correct instances + a planted false
analogy, and a migration_recipe whose instances are all superseded) and checks:

  1. false analogy : the planted wrong exemplifies link is flagged (false_analogy_rate);
  2. stale pattern : a pattern whose instances are all superseded is counted stale;
  3. transfer      : querying one instance surfaces its pattern + the analogous siblings;
  4. strategic     : a pattern node lands in the strategic tier of the pack.

Run:  python selftest_pattern.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import eval_retrieval as EV
import retrieve as R


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="amg-pat-"))
    try:
        labels = EV.build_pattern_demo(root)
        m = EV.pattern_metrics(root, labels)

        # 1. false analogy: exactly the planted wrong link is flagged (1 of 6 links)
        assert m["false_analogy_rate"] == round(1 / 6, 3), m
        assert [tuple(x) for x in m["false_links"]] == [
            ("code:src/valid.py::validate", "pattern:broad-except")], m["false_links"]
        print(f"PASS  false_analogy_rate flags the planted wrong link ({m['false_analogy_rate']})")

        # 2. stale pattern: legacy-auth-mig (all instances superseded) is stale, broad-except is not
        assert m["stale_pattern_rate"] == 0.5, m
        print(f"PASS  stale_pattern_rate counts the retired pattern ({m['stale_pattern_rate']})")

        # 3. transfer: aggregate recall of {pattern + analogues} holds
        assert m["transfer_recall"] is not None and m["transfer_recall"] >= 0.5, m
        print(f"PASS  transfer_recall surfaces pattern + analogues ({m['transfer_recall']})")

        # 4. strategic tier: a query about the anti-pattern puts the pattern node in strategic
        cfg = R.load_config(root)
        res = R.retrieve(root, "broad except Exception swallows the error", config=cfg,
                         write_pack=False, log_coactivation=False)
        assert "pattern:broad-except" in res["tiers"].get("strategic", []), res["tiers"]
        print("PASS  pattern node lands in the strategic tier")

        print("\nALL PATTERN-NODE CHECKS PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
