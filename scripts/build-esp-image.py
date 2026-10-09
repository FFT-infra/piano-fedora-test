#!/usr/bin/env python3
r"""制作 ESP 镜像：FAT32，写入 UEFI 启动文件与内核。

ESP 是 UEFI 从其中加载 Linux 的分区，格式 vfat，不需要挂载即可用
mtools（mformat/mcopy）离线写入，所以在任何环境都能跑。

固件按固定路径读取载荷（`PianoEspBootSource.c` 读 `\EFI\Piano\stable\boot.img`），
所以镜像内路径由调用方给定，工具不假设。

mtools 的 mmd 不补中间层，且对已存在的目录返回非零；两条都不是调用方能从
退出码看出来的，所以这里自己按层级建目录并跳过已建项。写入后逐个读回比对
sha256，把静默失败挡在交付之前。

用法：
    scripts/build-esp-image.py --files boot/BOOTAA64.EFI --kernel Image --dtb board.dtb \
        --size-mib 512 --output esp.img --manifest esp-manifest.json
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

MIB = 1024 * 1024
REQUIRED = ("mkfs.vfat", "mmd", "mcopy")


class EspError(Exception):
    """输入或环境不满足，属于可预期失败。"""


def require_tools():
    missing = [n for n in REQUIRED if shutil.which(n) is None]
    if missing:
        raise EspError("missing tools: " + ", ".join(missing) + " (install dosfstools, mtools)")


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(argv):
    return subprocess.run([str(a) for a in argv], check=True, capture_output=True)


def guest_directories(guests):
    """所有条目需要的目录，按层级从浅到深。

    mtools 的 mmd 不补中间层：`mmd ::/EFI/BOOT` 在 EFI 不存在时直接失败。
    上游 package_release.py 是一次把 `::/EFI ::/EFI/Piano ::/EFI/Piano/stable`
    全部传给 mmd。这里对全部条目去重后按深度排序，既不依赖调用方给出的顺序，
    也不会对同一个目录建两次（mmd 对已存在的目录返回非零）。
    """
    needed = set()
    for guest in guests:
        parts = Path(guest).parent.parts
        for depth in range(1, len(parts) + 1):
            needed.add("/".join(parts[:depth]))
    return sorted(needed, key=lambda value: (value.count("/"), value))


def read_back(target, guest, destination):
    """从镜像里取回刚写入的文件。"""
    run(["mcopy", "-i", target, "::/" + guest, destination])
    return sha256(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size-mib", type=int, default=512)
    parser.add_argument("--label", default="SUNUEFI_ESP")
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--epoch", type=int, default=315532800,
                        help="写入文件的 mtime（FAT 从 1980 起，默认 1980-01-01）")
    # 每个 --file 是 "镜像内路径:源文件"，可重复。
    parser.add_argument("--file", action="append", default=[],
                        help="target-path:source-file，可重复")
    args = parser.parse_args(argv)

    try:
        require_tools()
        if not 64 <= args.size_mib <= 2048:
            raise EspError(f"size must be 64..2048 MiB, got {args.size_mib}")
        if not args.file:
            raise EspError("at least one --file is required")
        if args.epoch < 315532800:
            raise EspError(f"epoch must be >= 315532800 (1980-01-01), got {args.epoch}")

        target = Path(args.output).resolve()
        if target.exists():
            raise EspError(f"output already exists: {target}")

        entries = []
        for spec in args.file:
            if ":" not in spec:
                raise EspError(f"bad --file {spec!r}; expected target-path:source-file")
            guest, source = spec.split(":", 1)
            guest = guest.lstrip("/")
            if not guest or ".." in Path(guest).parts:
                raise EspError(f"unsafe guest path: {guest!r}")
            src = Path(source).resolve()
            if not src.is_file():
                raise EspError(f"source not found: {src}")
            entries.append({"guest": guest, "source": str(src), "bytes": src.stat().st_size, "sha256": sha256(src)})

        with target.open("xb") as stream:
            stream.truncate(args.size_mib * MIB)
        # 4096 扇区：与本机 UFS 逻辑扇区一致。
        # --invariant 让同一输入产出同一镜像，便于核对。
        run(["mkfs.vfat", "--invariant", "-F", "32", "-n", args.label, "-S", "4096", target])

        directories = guest_directories(entry["guest"] for entry in entries)
        if directories:
            run(["mmd", "-i", target, *("::/" + name for name in directories)])

        with tempfile.TemporaryDirectory() as scratch:
            for entry in entries:
                # FAT 时间戳从 1980 起；固定 mtime 让镜像可复现。
                os.utime(entry["source"], (args.epoch,) * 2)
                run(["mcopy", "-m", "-i", target, entry["source"], "::/" + entry["guest"]])
                back = read_back(target, entry["guest"], Path(scratch) / Path(entry["guest"]).name)
                if back != entry["sha256"]:
                    raise EspError(f"readback differs for {entry['guest']}: {back} != {entry['sha256']}")

        listing = subprocess.run(["mdir", "-i", target, "-/", "-b"], check=True, capture_output=True).stdout.decode()
        result = {
            "status": "ESP_BUILT",
            "output": str(target),
            "size_mib": args.size_mib,
            "label": args.label,
            "sector_size": 4096,
            "epoch": args.epoch,
            "directories": directories,
            "files": entries,
            "readback_verified": True,
            "image_sha256": sha256(target),
            "bytes": target.stat().st_size,
            "listing": sorted(line for line in listing.splitlines() if line.strip()),
        }
        if args.manifest:
            Path(args.manifest).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except EspError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
