#!/usr/bin/env python3
"""省略文档时仅清理缺失的手册链接，保留运行时链接与有效手册。"""

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("finalizer", ROOT / "scripts/finalize-fedora-rootfs.py")
finalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalizer)


def fixture(root):
    alternatives = root / "etc/alternatives"
    man = root / "usr/share/man/man1"
    alternatives.mkdir(parents=True)
    man.mkdir(parents=True)
    (root / "usr/lib/modules/fixture").mkdir(parents=True)
    (alternatives / "apropos.1.gz").symlink_to("/usr/share/man/man1/apropos.man-db.1.gz")
    (man / "apropos.1.gz").symlink_to("/etc/alternatives/apropos.1.gz")
    (man / "present.1.gz").write_bytes(b"manual")
    (alternatives / "present-man").symlink_to("/usr/share/man/man1/present.1.gz")
    return alternatives, man


def main():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        alternatives, man = fixture(root)
        (alternatives / "runtime").symlink_to("/usr/bin/missing-runtime")
        removed = finalizer.prune_missing_manpage_links(root)
        assert sorted(removed) == ["etc/alternatives/apropos.1.gz", "usr/share/man/man1/apropos.1.gz"], removed
        assert not (alternatives / "apropos.1.gz").is_symlink()
        assert not (man / "apropos.1.gz").is_symlink()
        assert (alternatives / "present-man").is_symlink()
        assert (man / "present.1.gz").read_bytes() == b"manual"
        assert (alternatives / "runtime").is_symlink(), "runtime failures must remain visible to the packager"
        assert finalizer.prune_missing_manpage_links(root) == [], "cleanup must be idempotent"
    print("OK: missing manual links pruned; live manuals and runtime link guards preserved")


if __name__ == "__main__":
    main()
