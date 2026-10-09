#!/usr/bin/env python3
"""从 rootfs 目录制作 F2FS raw 镜像，并核对元数据。

上游 package_release.py 的 ext4() 用 mke2fs + debugfs，逐 inode 核对
uid/gid/mode/mtime 与 xattr。F2FS 没有等价的 debugfs，所以这里换一条路：
挂载真实 F2FS 后用归档工具写入，再读回逐项比对。这样 ACL/xattr/capability
走文件系统自身语义，不依赖 sload 是否复制它们。

为什么不用 sload -P：上游 f2fs-tools 的 sload 只保留 owner/mode，不枚举
源树的 xattr/ACL/capability。用它制镜像会静默丢元数据。

fsck.f2fs 的输出说明：f2fs-tools 1.16.0 对刚 mkfs 的干净镜像也会报
"fixing SIT types" 并写 checkpoint，退出码 0。这是该版本的正常行为，不是
镜像损坏。核对时看退出码，不把这类修复行当作失败。

用法：
    scripts/build-f2fs-image.py --rootfs DIR --size-mib 8192 --output OUT.img
    scripts/build-f2fs-image.py --rootfs DIR --size-mib 8192 --output OUT.img --verify
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tree_metadata import compare

MIB = 1024 * 1024
REQUIRED_TOOLS = ("mkfs.f2fs", "fsck.f2fs", "tar", "zstd")


class ImageError(Exception):
    """输入或环境不满足，属于可预期失败。"""


def require_tools():
    missing = [name for name in REQUIRED_TOOLS if shutil.which(name) is None]
    if missing:
        raise ImageError(
            "missing tools: " + ", ".join(missing)
            + " (install f2fs-tools, tar, zstd)"
        )


def require_mount_privilege():
    """挂载式导入需要 root。

    mount(8) 在权限不足时只返回 "failed to setup loop device"，看不出原因。
    这里提前判断并说清楚，避免把权限问题误读成镜像或工具问题。
    """
    if os.geteuid() != 0:
        raise ImageError(
            "mounting the image needs root; run this tool under sudo "
            f"(current euid {os.geteuid()})"
        )


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(argv, **kwargs):
    return subprocess.run([str(a) for a in argv], check=True, **kwargs)


def capture(argv):
    return subprocess.run([str(a) for a in argv], check=True, capture_output=True).stdout


def make_image(rootfs, target, size_mib, label, f2fs_options):
    """建 F2FS 镜像。不挂载：用 mkfs.f2fs 后由调用方决定导入方式。"""
    with target.open("xb") as stream:
        stream.truncate(size_mib * MIB)
    run(["mkfs.f2fs", "-f", "-l", label, *f2fs_options, target])
    return target


def import_via_mount(image, rootfs, work):
    """挂载镜像，用 tar 保属性写入，再卸载。

    需要 loop 挂载权限（root 或 userns）。失败时抛 ImageError，不静默降级。
    """
    mount = work / "mnt"
    mount.mkdir()
    run(["mount", "-o", "loop", image, mount])
    try:
        archive = work / "rootfs.tar.zst"
        run(["tar", "--format=pax", "--xattrs", "--xattrs-include=*", "--acls", "--selinux", "--numeric-owner", "-I", "zstd -3 -T0",
             "-cf", archive, "-C", rootfs, "."])
        run(["tar", "--xattrs", "--xattrs-include=*", "--acls", "--selinux", "--numeric-owner", "--same-owner",
             "-I", "zstd -T0", "-xf", archive, "-C", mount])
    finally:
        run(["umount", mount])


def verify(image, source_root, work):
    """卸载后只读检查，再只读挂载核对完整内容与元数据。"""
    before = sha256(image)
    fsck = subprocess.run(["fsck.f2fs", "-f", "--dry-run", str(image)],
                          capture_output=True, text=True, check=True)
    checks = [line for line in fsck.stdout.splitlines()
              if line.startswith("[FSCK]") and ("[Ok" in line or "[Fail" in line)]
    if not checks or any("[Fail" in line for line in checks):
        raise ImageError("fsck did not report successful consistency checks")
    if sha256(image) != before:
        raise ImageError("read-only fsck changed the image")
    mount = work / "verify"
    mount.mkdir()
    run(["mount", "-t", "f2fs", "-o", "loop,ro,norecovery", image, mount])
    try:
        metadata = compare(source_root, mount)
    finally:
        run(["umount", mount])
    return {**metadata, "fsck_exit": fsck.returncode,
            "fsck_read_only": True, "fsck_ran_unmounted": True,
            "fsck_changed_image": False, "fsck_flags": ["-f", "--dry-run"],
            "fsck_consistency_checks": len(checks),
            "fsck_tail": fsck.stdout.strip().splitlines()[-10:]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rootfs", required=True, help="已组装的 rootfs 目录")
    parser.add_argument("--size-mib", required=True, type=int, help="镜像容量，MiB")
    parser.add_argument("--output", required=True, help="输出 raw 镜像路径")
    parser.add_argument("--label", default="PIANOROOT", help="文件系统 label")
    parser.add_argument("--f2fs-option", action="append", default=[],
                        help="传给 mkfs.f2fs 的额外选项，可重复")
    parser.add_argument("--verify", action="store_true", help="制作后挂载读回核对")
    parser.add_argument("--manifest", help="把结果写入这个 JSON")
    args = parser.parse_args(argv)

    try:
        require_tools()
        rootfs = Path(args.rootfs).resolve()
        target = Path(args.output).resolve()
        if not rootfs.is_dir():
            raise ImageError(f"rootfs is not a directory: {rootfs}")
        if target.exists():
            raise ImageError(f"output already exists: {target}")
        if not 64 <= args.size_mib <= 65536:
            raise ImageError(f"size must be 64..65536 MiB, got {args.size_mib}")
        if not args.label or len(args.label) > 16:
            raise ImageError(f"label must be 1..16 chars, got {args.label!r}")
        # 挂载式导入是唯一的写入路径，提前确认权限，不等到 mount 才报含糊错误。
        require_mount_privilege()

        result = {
            "status": "IMAGE_PLANNED",
            "rootfs": str(rootfs),
            "output": str(target),
            "size_mib": args.size_mib,
            "label": args.label,
            "format": "raw-f2fs",
            "f2fs_options": args.f2fs_option,
            "device_tested": False,
        }

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            make_image(rootfs, target, args.size_mib, args.label, args.f2fs_option)
            import_via_mount(target, rootfs, work)
            result["status"] = "IMAGE_BUILT"
            if args.verify:
                result["verify"] = verify(target, rootfs, work)
                result["status"] = "IMAGE_VERIFIED"

        result["sha256"] = sha256(target)
        result["bytes"] = target.stat().st_size
        if args.manifest:
            Path(args.manifest).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ImageError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
