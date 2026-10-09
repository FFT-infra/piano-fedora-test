#!/usr/bin/env python3
"""离线检查 rootfs 载荷脚本：模块安装、硬件入口、拒绝路径。

这是引导脚本的两条硬前置条件：`/sysroot/usr/lib/modules/$release` 与
`/sysroot/usr/lib/piano/piano-disk-hardware-prepare`。缺任一条启动就进救援。
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "stage-rootfs-payload.py"
RELEASE = "7.2.9-fixture"

# 与研究仓库同级；不存在时只跳过需要真实 SunUEFI 的用例。
SUNUEFI = ROOT.parent / "mipad8p-piano" / "sources" / "Project-SunUEFI"


def run(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=ROOT,
    )


def make_rootfs(tmp):
    """建一个像 Fedora 的树：/lib 与 /bin 是指向 /usr 的符号链接。"""
    rootfs = tmp / "rootfs"
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "lib").symlink_to("usr/lib")
    (rootfs / "bin").symlink_to("usr/bin")
    return rootfs


def make_kernel(tmp):
    kernel = tmp / "kernel"
    modules = kernel / "modules" / "lib" / "modules" / RELEASE / "kernel" / "drivers" / "iommu"
    modules.mkdir(parents=True)
    (modules / "arm-smmu.ko").write_bytes(b"placeholder-module\n")
    (kernel / "manifest.json").write_text(json.dumps({
        "kernel_release": RELEASE,
        "source_commit": "352508459733d3e6d349ea5581a8dd2fd8bb4180",
    }))
    return kernel


def main():
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    # 输入不存在：拒绝。
    check(run("--rootfs", "/nonexistent", "--kernel", "/nonexistent",
              "--sunuefi", "/nonexistent").returncode == 2,
          "missing inputs should be rejected")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rootfs = make_rootfs(tmp)
        kernel = make_kernel(tmp)
        # 通用拒绝路径用自带 fixture，CI 无须具有研究仓库的兄弟目录。
        preflight_source = tmp / "preflight-source"
        preflight_source.mkdir()
        (preflight_source / "build.sh").write_text("#!/bin/sh\n")

        # 空目录不算系统树。
        empty = tmp / "empty"
        empty.mkdir()
        check(run("--rootfs", str(empty), "--kernel", str(kernel),
                  "--sunuefi", str(preflight_source)).returncode == 2,
              "a directory without usr/ should be rejected")

        # 内核 manifest 缺失：拒绝。
        bare = tmp / "bare-kernel"
        bare.mkdir()
        check(run("--rootfs", str(rootfs), "--kernel", str(bare),
                  "--sunuefi", str(preflight_source)).returncode == 2,
              "a kernel artifact without manifest should be rejected")

        # 目标穿过符号链接：拒绝。
        #
        # Fedora 的 /lib 与 /bin 指向 /usr，写错位置会静默落到宿主或别处。
        tricky = make_rootfs(tmp / "tricky")
        (tricky / "usr" / "lib").symlink_to("/etc")
        result = run("--rootfs", str(tricky), "--kernel", str(kernel),
                     "--sunuefi", str(preflight_source))
        check(result.returncode == 2 and "symlink" in result.stderr,
              f"a destination through a symlink should be rejected: {result.stderr.strip()}")

        if not (SUNUEFI / "build.sh").is_file():
            if failures:
                for item in failures:
                    print(f"FAIL: {item}", file=sys.stderr)
                return 1
            print("SKIP: SunUEFI checkout unavailable; reject paths checked only "
                  "(not a pass)")
            return 0

        # 真实路径：模块与硬件入口都要落位。
        good = make_rootfs(tmp / "good")
        result = run("--rootfs", str(good), "--kernel", str(kernel),
                     "--sunuefi", str(SUNUEFI))
        if result.returncode != 0:
            check(False, f"staging failed: {result.stderr.strip()}")
        else:
            modules = good / "usr" / "lib" / "modules" / RELEASE
            check(modules.is_dir(), "the kernel release tree must be staged")
            check(any(modules.rglob("*.ko")), "staged modules must include .ko files")
            # 开发链接不能进镜像。
            for name in ("build", "source"):
                check(not (modules / name).exists(),
                      f"development link {name} must not be staged")

            prepare = good / "usr" / "lib" / "piano" / "piano-ram-hardware-prepare"
            check(prepare.is_file(), "the hardware prepare program must be staged")
            check(prepare.stat().st_mode & 0o111, "hardware prepare must be executable")
            alias = good / "usr" / "lib" / "piano" / "piano-disk-hardware-prepare"
            check(alias.is_symlink(), "the disk-hardware-prepare alias must exist")
            check(alias.readlink().name == "piano-ram-hardware-prepare",
                  "the alias must point at the prepare program")
            for name in ("piano_dma_routes.py", "piano_dma_contexts.py"):
                check((good / "usr" / "lib" / "piano" / name).is_file(),
                      f"{name} must be staged for the prepare program")

            # 引导脚本的两条前置条件都要满足。
            check(modules.is_dir(),
                  "bootstrap precondition: matching module tree present")
            check(alias.exists(),
                  "bootstrap precondition: hardware prepare executable present")

            provenance = json.loads(
                (good / "usr" / "share" / "piano-provenance"
                 / "kernel-modules.json").read_text()
            )
            check(provenance["kernel_release"] == RELEASE,
                  "provenance must record the kernel release")
            check(provenance["device_tested"] is False,
                  "must not claim device testing")

        # 同一个 release 重复安装：拒绝。
        check(run("--rootfs", str(good), "--kernel", str(kernel),
                  "--sunuefi", str(SUNUEFI)).returncode == 2,
              "re-staging the same release should be rejected")

        # 校验器摘要漂移：拒绝。
        tampered = tmp / "sunuefi"
        shutil.copytree(SUNUEFI, tampered, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "build", "artifacts"))
        checker = tampered / "tools" / "check_piano_kernel_dma_routes.py"
        checker.write_bytes(checker.read_bytes() + b"\n# tampered\n")
        drift = make_rootfs(tmp / "drift")
        result = run("--rootfs", str(drift), "--kernel", str(kernel),
                     "--sunuefi", str(tampered))
        check(result.returncode == 2 and "digest changed" in result.stderr,
              f"a drifted checker must be rejected: {result.stderr.strip()}")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print("OK: rootfs payload staging, bootstrap preconditions and reject paths behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
