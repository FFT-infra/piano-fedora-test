#!/usr/bin/env python3
"""忽略构建源码中的断链，同时继续拒绝项目文档的真实断链。"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", type=Path, default=ROOT / "tests/check_links.py")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "tests").mkdir()
        shutil.copyfile(args.tool, root / "tests/check_links.py")
        (root / ".gitignore").write_text("/build/\n/out/\n")
        (root / "README.md").write_text("[valid](target.md)\n")
        (root / "target.md").write_text("Project documentation\n")
        (root / "build/reference").mkdir(parents=True)
        (root / "build/reference/README.md").write_text("[upstream-only](not-downloaded.md)\n")
        good = subprocess.run([sys.executable, str(root / "tests/check_links.py")], capture_output=True, text=True)
        if good.returncode:
            print("FAIL: ignored upstream references entered the project link check", file=sys.stderr)
            print(good.stderr, file=sys.stderr)
            return 1
        (root / "README.md").write_text("[broken](missing-project-doc.md)\n")
        bad = subprocess.run([sys.executable, str(root / "tests/check_links.py")], capture_output=True, text=True)
        if bad.returncode != 1 or "missing-project-doc.md" not in bad.stderr:
            print("FAIL: real project broken link was not rejected", file=sys.stderr)
            return 1
    print("OK: generated references excluded and genuine project broken links rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
