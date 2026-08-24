"""Submission honesty and budget checks.

Confirms that every metric quoted in README.md matches the sealed results.json, that the test
suite stays inside its 14-file budget, that dependency specifiers remain open, and that no
project pyproject.toml was introduced.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS = PROJECT_ROOT / "reports" / "heldout_test" / "results.json"
README = PROJECT_ROOT / "README.md"
TEST_FILE_BUDGET = 14
SPECIFIER = re.compile(r"[=<>~!]")


def _quoted_numbers(text: str) -> set[str]:
    """Collect numeric literals from README tables, normalised for comparison."""

    return {
        match.group(0).replace(",", "")
        for match in re.finditer(r"\d[\d,]*\.\d+|\b\d[\d,]{2,}\b", text)
    }


def _numeric_leaves(node: object) -> list[float]:
    if isinstance(node, bool):
        return []
    if isinstance(node, (int, float)):
        return [float(node)]
    if isinstance(node, dict):
        return [value for item in node.values() for value in _numeric_leaves(item)]
    if isinstance(node, list):
        return [value for item in node for value in _numeric_leaves(item)]
    return []


def _renderings(values: list[float]) -> set[str]:
    """Every string form a sealed number could legitimately take in prose or a table."""

    rendered: set[str] = set()
    for value in values:
        rendered.add(repr(value))
        for spec in (".1f", ".2f", ".3f", ".4f", ",.2f"):
            rendered.add(format(value, spec).replace(",", ""))
        if float(value).is_integer():
            rendered.add(str(int(value)))
    return rendered


def check() -> dict[str, object]:
    if not RESULTS.exists():
        raise SystemExit("Sealed results.json is missing; run evaluation/heldout_report.py first.")
    report = json.loads(RESULTS.read_text(encoding="utf-8"))
    readme = README.read_text(encoding="utf-8")

    selected = report["transaction"][report["transaction"]["selected_operating_point"]]
    metrics = selected["metrics"]
    events = report["events"]["metrics"]
    business = report["business"]

    expected = {
        "pr_auc": f"{metrics['pr_auc']:.4f}",
        "roc_auc": f"{metrics['roc_auc']:.4f}",
        "recall": f"{metrics['recall']:.4f}",
        "precision": f"{metrics['precision']:.4f}",
        "f1": f"{metrics['f1']:.4f}",
        "threshold": f"{selected['threshold']:.4f}",
        "true_positives": str(metrics["true_positives"]),
        "false_positives": str(metrics["false_positives"]),
        "false_negatives": str(metrics["false_negatives"]),
        "true_negatives": str(metrics["true_negatives"]),
        "event_recall": f"{events['recall']:.3f}",
        "event_precision": f"{events['precision']:.3f}",
        "median_delay": f"{events['median_delay_minutes']:.1f}",
        "fraud_captured": f"{business['fraud_exposure_captured_inr']:,.2f}",
        "fraud_missed": f"{business['fraud_loss_missed_inr']:,.2f}",
        "fp_cost": f"{business['false_positive_cost_inr']:,.2f}",
        "legitimate_disrupted": f"{business['legitimate_value_disrupted_inr']:,.2f}",
        "net_risk_benefit": f"{business['net_risk_benefit_inr']:,.2f}",
    }
    missing_from_readme = {
        name: value for name, value in expected.items() if value not in readme
    }

    # Any number in the README that cannot be traced to the sealed report is suspicious.
    traceable = _renderings(_numeric_leaves(report)) | _quoted_numbers(json.dumps(report))
    known_context = {
        # Dataset facts measured in Phase 1 and quoted in the problem statement.
        "13.9", "1.69", "1.83", "13.7", "22.3", "0.8", "2.3", "2.9", "2.6", "2.2",
        # Design constants and documented cost assumptions.
        "250", "500", "120", "15", "0.90", "40",
        # Model IDs, ports, and replay speed referenced in prose and commands.
        "3.5", "3.1", "4", "31", "8000", "3000", "5000", "300", "600", "16", "7",
    }
    untraceable = sorted(
        value
        for value in _quoted_numbers(readme)
        if value not in traceable and value not in known_context
    )

    test_files = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for path in (PROJECT_ROOT / "tests").rglob("test_*.py")
    )

    requirement_files = sorted((PROJECT_ROOT / "requirements").glob("*.txt"))
    pinned: list[str] = []
    for path in requirement_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if not entry or entry.startswith(("#", "-r")):
                continue
            if SPECIFIER.search(entry):
                pinned.append(f"{path.name}: {entry}")

    project_pyproject = (PROJECT_ROOT / "pyproject.toml").exists()

    return {
        "sealed_label_reads": report["integrity"]["access_log_entries"],
        "readme_metrics_checked": len(expected),
        "readme_metrics_missing": missing_from_readme,
        "readme_untraceable_numbers": untraceable,
        "test_files": len(test_files),
        "test_file_budget": TEST_FILE_BUDGET,
        "test_files_over_budget": max(0, len(test_files) - TEST_FILE_BUDGET),
        "pinned_requirements": pinned,
        "project_pyproject_present": project_pyproject,
    }


def main() -> int:
    result = check()
    print(json.dumps(result, indent=2))
    failed = (
        bool(result["readme_metrics_missing"])
        or bool(result["readme_untraceable_numbers"])
        or result["test_files_over_budget"]
        or bool(result["pinned_requirements"])
        or result["project_pyproject_present"]
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
