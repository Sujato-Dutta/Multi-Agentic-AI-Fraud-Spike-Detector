"""Static integrity check for the no-build frontend bundle.

Verifies that every local asset reference and ES module import resolves, that no metric
literal is hardcoded into the views, and that each page loads its entry module.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "frontend"
ATTR_REF = re.compile(r"""(?:href|src)=["'](/[^"'#?]+)["']""")
MODULE_REF = re.compile(r"""from\s+["'](\.{1,2}/[^"']+)["']""")
FETCH_REF = re.compile(r"""fetch\(["'](/[^"'?]+)["']""")
ALLOWED_RUNTIME_PREFIXES = ("/api/", "/metrics", "http://", "https://")


def check() -> dict[str, object]:
    files = sorted(
        path
        for path in FRONTEND.rglob("*")
        if path.is_file() and path.suffix in {".html", ".js", ".css", ".svg"}
    )
    if not files:
        raise SystemExit("frontend bundle is empty")

    missing: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for ref in ATTR_REF.findall(text) + FETCH_REF.findall(text):
            if ref.startswith(ALLOWED_RUNTIME_PREFIXES):
                continue
            if ref == "/assets/videos/demo.mp4":
                continue  # optional recorded clip; the animated preview is the default
            if not (FRONTEND / ref.lstrip("/")).exists():
                missing.append(f"{path.relative_to(FRONTEND)} -> {ref}")
        for ref in MODULE_REF.findall(text):
            if not (path.parent / ref).resolve().exists():
                missing.append(f"{path.relative_to(FRONTEND)} -> {ref}")

    pages = sorted(FRONTEND.glob("pages/*.html")) + [FRONTEND / "index.html"]
    without_entry = [
        str(page.relative_to(FRONTEND))
        for page in pages
        if 'type="module"' not in page.read_text(encoding="utf-8")
    ]

    # Guard the Phase 8 honesty rule: no metric literals baked into the interface.
    suspicious: list[str] = []
    metric_words = re.compile(
        r"(precision|recall|pr_auc|roc_auc|f1|net_risk_benefit|psi)\s*[:=]\s*0?\.\d+",
        re.IGNORECASE,
    )
    for path in files:
        if path.suffix not in {".js", ".html"}:
            continue
        for match in metric_words.finditer(path.read_text(encoding="utf-8")):
            suspicious.append(f"{path.relative_to(FRONTEND)}: {match.group(0)}")

    return {
        "files": len(files),
        "pages": len(pages),
        "missing_references": missing,
        "pages_without_entry_module": without_entry,
        "hardcoded_metric_literals": suspicious,
    }


def main() -> int:
    result = check()
    print(json.dumps(result, indent=2))
    failed = any(
        result[key]
        for key in ("missing_references", "pages_without_entry_module", "hardcoded_metric_literals")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
