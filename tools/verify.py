from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    ("compile", (sys.executable, "-m", "compileall", "-q", "src", "tests")),
    ("tests", (sys.executable, "-m", "pytest", "-q")),
    ("lint", (sys.executable, "-m", "ruff", "check", ".")),
    ("unstaged whitespace", ("git", "diff", "--check")),
    ("staged whitespace", ("git", "diff", "--cached", "--check")),
)


def main() -> int:
    for label, command in CHECKS:
        print(f"==> {label}", flush=True)
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
