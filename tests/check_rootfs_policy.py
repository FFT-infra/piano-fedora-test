#!/usr/bin/env python3
"""离线检查 F2FS rootfs 策略：写入内容与拒绝路径。"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "apply-rootfs-policy.py"


def run(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=ROOT,
    )


def main():
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    # 目录不存在：拒绝。
    check(run("--rootfs", "/nonexistent-rootfs").returncode == 2,
          "a missing rootfs should be rejected")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 空目录：拒绝（不像一套系统树）。
        empty = tmp / "empty"
        empty.mkdir()
        check(run("--rootfs", str(empty)).returncode == 2,
              "a directory without usr/ should be rejected")

        rootfs = tmp / "rootfs"
        (rootfs / "usr").mkdir(parents=True)
        result = run("--rootfs", str(rootfs))
        check(result.returncode == 0, f"apply should succeed: {result.stderr.strip()}")
        if result.returncode == 0:
            fstab = (rootfs / "etc" / "fstab").read_text()
            # F2FS 挂载项必须在，且不能留 ext4 或 growfs。
            check("f2fs" in fstab, "fstab must mount the root as f2fs")
            check("ext4" not in fstab, "fstab must not reference ext4")
            check("x-systemd.growfs" not in fstab,
                  "F2FS has no systemd growfs support; capacity comes from the image")

            rules = (rootfs / "etc" / "udev" / "rules.d"
                     / "01-piano-protect-android.rules").read_text()
            check('ENV{ID_FS_TYPE}=="f2fs"' in rules, "udev rule must allow f2fs")
            check('ENV{ID_FS_TYPE}=="ext4"' not in rules, "udev rule must not require ext4")
            # 保护规则必须保留：其余 UFS 分区仍设只读。
            check("blockdev --setro" in rules,
                  "Android partition protection must stay in place")
            check("PIANOROOT" in rules and "sunuefi_root" in rules,
                  "only the piano root may be released")

            hook = (rootfs / "usr" / "lib" / "systemd" / "system-shutdown"
                    / "f2fs-root-shutdown")
            check(hook.is_file(), "the F2FS shutdown hook must be installed")
            if hook.is_file():
                check(hook.stat().st_mode & 0o111, "the shutdown hook must be executable")
                text = hook.read_text()
                check("f2fs_io" in text and "shutdown 0" in text,
                      "the hook must call f2fs_io shutdown")

            policy = json.loads((rootfs / "etc" / "piano" / "root-policy.json").read_text())
            check(policy["root_fstype"] == "f2fs", "policy must record f2fs")
            check(policy["root_label"] == "PIANOROOT", "policy must record the piano label")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print("OK: F2FS rootfs policy content and reject paths behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
