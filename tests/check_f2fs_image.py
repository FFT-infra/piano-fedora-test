#!/usr/bin/env python3
"""离线检查 F2FS 镜像工具的参数校验与拒绝路径。

真实制作需要 root 与 f2fs-tools，这里只测不依赖它们的部分。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "build-f2fs-image.py"


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

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rootfs = tmp / "rootfs"
        rootfs.mkdir()
        (rootfs / "etc").mkdir()
        (rootfs / "etc" / "os-release").write_text("ID=fedora\n")

        # rootfs 不存在：拒绝。
        check(
            run("--rootfs", str(tmp / "nope"), "--size-mib", "64",
                "--output", str(tmp / "a.img")).returncode == 2,
            "missing rootfs should be rejected",
        )

        # 容量越界：拒绝。
        check(
            run("--rootfs", str(rootfs), "--size-mib", "8",
                "--output", str(tmp / "b.img")).returncode == 2,
            "size below 64 MiB should be rejected",
        )
        check(
            run("--rootfs", str(rootfs), "--size-mib", "99999",
                "--output", str(tmp / "c.img")).returncode == 2,
            "size above 65536 MiB should be rejected",
        )

        # label 过长：拒绝。
        check(
            run("--rootfs", str(rootfs), "--size-mib", "64",
                "--output", str(tmp / "d.img"),
                "--label", "WAY-TOO-LONG-LABEL").returncode == 2,
            "overlong label should be rejected",
        )

        # 输出已存在：拒绝。
        existing = tmp / "e.img"
        existing.write_bytes(b"")
        check(
            run("--rootfs", str(rootfs), "--size-mib", "64",
                "--output", str(existing)).returncode == 2,
            "existing output should be rejected",
        )

        # 缺 f2fs-tools：明确报缺哪个工具，而不是含糊失败。
        result = run("--rootfs", str(rootfs), "--size-mib", "64",
                     "--output", str(tmp / "f.img"))
        if result.returncode == 2 and "missing tools" in result.stderr:
            check(True, "")
        elif result.returncode == 0:
            # 环境里有 f2fs-tools，真实制作成功了。
            check((tmp / "f.img").is_file(), "successful run should create the image")
        else:
            check(False, f"unexpected failure: {result.stderr.strip()}")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print("OK: f2fs image argument and reject paths behave as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
