#!/usr/bin/env python3
"""离线检查 ESP 镜像工具的参数校验与拒绝路径。"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "build-esp-image.py"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT)


def main():
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "BOOTAA64.EFI"
        src.write_bytes(b"MZ" + b"\0" * 1022)

        # 没有 --file：拒绝。
        check(run("--output", str(tmp / "a.img")).returncode == 2, "no file should be rejected")

        # 格式错误的 --file：拒绝。
        check(
            run("--output", str(tmp / "b.img"), "--file", "no-colon").returncode == 2,
            "malformed --file should be rejected",
        )

        # 源文件不存在：拒绝。
        check(
            run("--output", str(tmp / "c.img"), "--file", "EFI/BOOT/BOOTAA64.EFI:/nope").returncode == 2,
            "missing source should be rejected",
        )

        # 容量越界：拒绝。
        check(
            run("--output", str(tmp / "d.img"), "--size-mib", "16",
                "--file", f"EFI/BOOT/BOOTAA64.EFI:{src}").returncode == 2,
            "size below 64 should be rejected",
        )

        # 输出已存在：拒绝。
        existing = tmp / "e.img"
        existing.write_bytes(b"")
        check(
            run("--output", str(existing), "--file", f"EFI/BOOT/BOOTAA64.EFI:{src}").returncode == 2,
            "existing output should be rejected",
        )

        # 正常路径：工具齐备则产出镜像。
        result = run("--output", str(tmp / "ok.img"), "--size-mib", "64",
                     "--file", f"EFI/BOOT/BOOTAA64.EFI:{src}")
        if result.returncode == 0:
            check((tmp / "ok.img").is_file(), "successful run should create the image")
        elif "missing tools" in result.stderr:
            pass  # 环境缺 dosfstools/mtools，明确报缺即算通过。
        else:
            check(False, f"unexpected failure: {result.stderr.strip()}")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print("OK: esp image argument and reject paths behave as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
