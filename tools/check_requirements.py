"""
Fail if the code imports something requirements.txt does not install.

Written the morning after Pillow was added to a local virtualenv and not to
requirements. Everything worked on the machine it was written on; the daily run
imported run.py on a clean checkout, hit `from PIL import ...`, and died in one
second. The board went a day without updating for a missing line in a text file.

Import errors are the cheapest class of failure to prevent and among the most
expensive to notice, because nothing is wrong until the one environment that
matters is built from scratch.

    python tools/check_requirements.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Import name -> the distribution that provides it, where they differ.
DISTRIBUTIONS = {
    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "dateutil": "python-dateutil",
}


def imported() -> set[str]:
    """Every third-party module the code imports."""
    found: set[str] = set()
    for path in [ROOT / "run.py", *(ROOT / "src").glob("*.py"),
                 *(ROOT / "tools").glob("*.py")]:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    local = {p.stem for p in (ROOT / "src").glob("*.py")} | {"src", "tools"}
    return {m for m in found if m not in sys.stdlib_module_names and m not in local}


def required() -> set[str]:
    """The distributions requirements.txt installs, lowercased."""
    text = (ROOT / "requirements.txt").read_text()
    names = re.findall(r"^\s*([A-Za-z0-9_.-]+)", text, re.M)
    return {n.lower().replace("_", "-") for n in names}


def main() -> int:
    have = required()
    missing = sorted(
        m for m in imported()
        if DISTRIBUTIONS.get(m, m).lower().replace("_", "-") not in have
    )
    if missing:
        print("requirements.txt does not install:", file=sys.stderr)
        for m in missing:
            print(f"  {m}  (add {DISTRIBUTIONS.get(m, m)})", file=sys.stderr)
        return 1
    print(f"  requirements cover all {len(imported())} third-party imports")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
