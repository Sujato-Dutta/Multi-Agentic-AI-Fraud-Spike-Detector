"""Project evaluation entry point. Phase 1 exposes contract checks only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.leakage_check import run_leakage_checks
from evaluation.metrics import metric_contract_check
from evaluation.phase2_benchmark import run_phase2_benchmark


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Run leakage and metric contracts")
    parser.add_argument("--phase2", action="store_true", help="Run validation and dev-test benchmark")
    args = parser.parse_args()
    if args.check == args.phase2:
        parser.error("Choose exactly one of --check or --phase2")
    if args.phase2:
        report = run_phase2_benchmark()
        validation = report["validation"]["spikes"]["metrics"]
        dev_test = report["dev_test"]["spikes"]["metrics"]
        print(json.dumps({"status": "ok", "validation": validation, "dev_test": dev_test}, indent=2))
        return 0
    leakage = run_leakage_checks()
    metric_contract_check()
    print(json.dumps({"status": "ok", "leakage": leakage.to_dict()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
