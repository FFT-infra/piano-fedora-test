#!/usr/bin/env python3
"""校验仓库内 Markdown 的相对链接可解析。"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)]+)\)")
SKIP = ("http://", "https://", "mailto:", "#")


def main():
    problems = []
    files = [p for p in ROOT.rglob("*.md") if ".git" not in p.parts]
    for path in files:
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            target = target.strip()
            if not target or target.startswith(SKIP):
                continue
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                problems.append(f"{path.relative_to(ROOT)}: {target}")

    if problems:
        for item in problems:
            print(f"FAIL: broken link {item}", file=sys.stderr)
        return 1
    print(f"OK: {len(files)} markdown files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
