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


def launcher_fixture(root):
    binary = root / "usr/bin"
    target = root / "usr/share/org.gnome.Weather/org.gnome.Weather"
    binary.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"weather launcher")
    (binary / "gnome-weather").symlink_to("../../../../../../../usr/share/org.gnome.Weather/org.gnome.Weather")
    return binary, target


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
        binary, target = launcher_fixture(root)
        changes = finalizer.normalize_launcher_links(root)
        assert set(changes) == {"usr/bin/gnome-weather"}, changes
        assert (binary / "gnome-weather").resolve() == target
        assert (binary / "gnome-weather").read_bytes() == b"weather launcher"
        assert finalizer.normalize_launcher_links(root) == {}
        (binary / "bad").symlink_to("../../../etc/passwd")
        try:
            finalizer.normalize_launcher_links(root)
        except ValueError:
            pass
        else:
            raise AssertionError("an unexpected runtime escape was normalized")
        assert (binary / "bad").is_symlink()
    print("OK: missing manuals pruned, launcher normalized and runtime path guards preserved")


if __name__ == "__main__":
    main()
