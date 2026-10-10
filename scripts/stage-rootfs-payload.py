#!/usr/bin/env python3
"""把内核模块和 piano 硬件准备装进 Fedora rootfs。

这是 Fedora 后端缺的那一环。SunUEFI 在 `build_release_rootfs.py` 里做了三件事，
Debian 版才有：

1. `stage_piano_kernel_modules.py` 把内核产物里的模块装进
   `usr/lib/modules/<release>`。缺了它，引导脚本第 126 行的
   `[ -d /sysroot/usr/lib/modules/$release ]` 会失败，启动直接进救援。
2. `stage_piano_ram_hardware.py` 放 `piano-ram-hardware-prepare` 和它依赖的
   两个校验模块。缺了它，第 128 行的可执行检查会失败。
3. `apply_policy()` 写 fstab、root-policy.json 和 udev 保护规则。这三样由
   `apply-rootfs-policy.py` 负责，本脚本不重复。

做法与上游一致：不 fork，从固定 commit 的 SunUEFI checkout 里读源文件，
逐个核对摘要后复制。摘要不符就报错，不静默使用漂移的内容。

用法：
    scripts/stage-rootfs-payload.py --rootfs DIR --kernel DIR --sunuefi DIR
"""

import argparse
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from piano_artifacts import inspect_kernel, safe_destination

ROOT = Path(__file__).resolve().parents[1]

# 与 SunUEFI tools/stage_piano_ram_hardware.py 里登记的摘要一致。
# 这些文件是启动路径上的 DMA 校验器，内容变了必须重新审阅，不能自动跟随。
CHECKER_SHA256 = "1f2c26329c00b5b791a101d409032cd3d8b1962f83018937807c5cbbb7f5a2e1"
CONTEXT_CHECKER_SHA256 = "9731f4027c0656d50a930ed2fb087d0ccfb3a99bb4373cfb65e9e0a0865ac63d"

CHECKER = "tools/check_piano_kernel_dma_routes.py"
CONTEXT_CHECKER = "tools/check_piano_kernel_contexts.py"
HARDWARE_PREPARE = "linux/userspace/piano-ram-hardware-prepare"


class StageError(Exception):
    """输入或环境不满足，属于可预期失败。"""


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_target(root, relative):
    """目标路径不能穿过符号链接。

    Fedora 的 /lib 与 /bin 是指向 /usr 的符号链接，直接写会写到宿主或写到
    错误位置。与上游 `safe_target` 同构。
    """
    path = root / relative
    for current in (path, *path.parents):
        if current == root:
            break
        if current.is_symlink():
            raise StageError(f"destination traverses a symlink: {relative}")
    return path


def load_kernel(kernel):
    manifest = kernel / "manifest.json"
    if not manifest.is_file():
        raise StageError(f"missing kernel manifest: {manifest}")
    record, _ = inspect_kernel(kernel)
    release = record.get("kernel_release")
    if not isinstance(release, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", release):
        raise StageError(f"invalid kernel release: {release!r}")
    modules = kernel / "modules" / "lib" / "modules" / release
    if not modules.is_dir():
        raise StageError(f"missing kernel modules in the artifact: {modules}")
    return record, release, modules


def stage_modules(rootfs, kernel, release, modules):
    destination = safe_target(rootfs, Path("usr/lib/modules") / release)
    if destination.exists() or destination.is_symlink():
        raise StageError(f"kernel release already staged: {release}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(modules, destination, symlinks=True)
    # 开发用的 build/source 链接指向构建机路径，运行时不适用。
    for name in ("build", "source"):
        link = destination / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise StageError(f"unexpected real development directory: {name}")
    return destination


def stage_hardware(rootfs, sunuefi):
    """放硬件准备程序与它依赖的两个校验器。

    摘要按 SunUEFI 自己登记的 pin 核对；不符说明上游的校验器变了，
    需要重新审阅后才能放行。
    """
    sources = {
        "usr/lib/piano/piano_dma_routes.py": (sunuefi / CHECKER, CHECKER_SHA256),
        "usr/lib/piano/piano_dma_contexts.py": (sunuefi / CONTEXT_CHECKER, CONTEXT_CHECKER_SHA256),
        "usr/lib/piano/piano-ram-hardware-prepare": (
            sunuefi / HARDWARE_PREPARE,
            "a171ece910b51b7a9586c58277df78957709a4fff559245c9919d3f8432c925c"),
        # Fedora 44 的 /usr/local/sbin 是 bin 兼容链接；写 canonical 路径。
        "usr/local/bin/piano-debug-bootstrap": (
            sunuefi / "linux/userspace/piano-debug-bootstrap",
            "d201ec73a70f3db8d80a9e424fe88ec4de90a31bf2cba49d1229970ea49427ba"),
        "usr/lib/piano/piano-boot-task-snapshot": (
            sunuefi / "linux/userspace/piano-boot-task-snapshot", None),
    }
    staged = {}
    for relative, (source, expected) in sources.items():
        if not source.is_file():
            raise StageError(f"missing SunUEFI source: {source}")
        actual = sha256(source)
        if expected is not None and actual != expected:
            raise StageError(
                f"{source.name}: digest changed\n"
                f"  expected {expected}\n  actual   {actual}\n"
                "  校验器内容变了，需要重新审阅后再放行。"
            )
        target = safe_target(rootfs, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        # 可执行的是 prepare 本体，两个 .py 是它 import 的模块。
        target.chmod(0o755 if not relative.endswith(".py") else 0o644)
        staged[relative] = actual
    # 引导脚本按 /usr/lib/piano/piano-disk-hardware-prepare 调用它。
    alias = safe_target(rootfs, "usr/lib/piano/piano-disk-hardware-prepare")
    if alias.exists() or alias.is_symlink():
        alias.unlink()
    alias.symlink_to("piano-ram-hardware-prepare")
    return staged


def stage(rootfs, kernel, sunuefi):
    rootfs = Path(rootfs).resolve()
    kernel = Path(kernel).resolve()
    sunuefi = Path(sunuefi).resolve()
    if not rootfs.is_dir():
        raise StageError(f"rootfs is not a directory: {rootfs}")
    if not (rootfs / "usr").is_dir():
        raise StageError(f"rootfs does not look like a system tree: {rootfs}")
    if not (sunuefi / "build.sh").is_file():
        raise StageError(f"not a SunUEFI checkout: {sunuefi}")

    safe_destination(rootfs, "usr/lib/modules")

    record, release, modules = load_kernel(kernel)
    destination = stage_modules(rootfs, kernel, release, modules)
    staged = stage_hardware(rootfs, sunuefi)

    module_files = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*.ko")
    )
    if not module_files:
        raise StageError("staged module tree contains no modules")

    provenance = safe_target(rootfs, "usr/share/piano-provenance/kernel-modules.json")
    provenance.parent.mkdir(parents=True, exist_ok=True)
    provenance.write_text(json.dumps({
        "kernel_release": release,
        "kernel_commit": record.get("source_commit"),
        "kernel_manifest_sha256": sha256(kernel / "manifest.json"),
        "kernel_config_sha256": record["config_sha256"],
        "module_count": len(module_files),
        "hardware_prepare": staged,
        "device_tested": False,
    }, indent=2) + "\n", encoding="utf-8")

    return {
        "status": "ROOTFS_PAYLOAD_STAGED_NOT_BOOT_VERIFIED",
        "rootfs": str(rootfs),
        "kernel_release": release,
        "kernel_commit": record.get("source_commit"),
        "modules_installed": len(module_files),
        "modules_path": str(destination.relative_to(rootfs)),
        "hardware_prepare": sorted(staged),
        "hardware_prepare_alias": "usr/lib/piano/piano-disk-hardware-prepare",
        "device_tested": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rootfs", required=True)
    parser.add_argument("--kernel", required=True, help="内核 artifact 目录")
    parser.add_argument("--sunuefi", required=True, help="SunUEFI checkout（补丁已应用）")
    parser.add_argument("--manifest", help="把结果写入这个 JSON")
    args = parser.parse_args(argv)

    try:
        result = stage(args.rootfs, args.kernel, args.sunuefi)
        if args.manifest:
            Path(args.manifest).write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (StageError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, shutil.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
