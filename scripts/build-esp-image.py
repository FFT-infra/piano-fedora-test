#!/usr/bin/env python3
"""制作 ESP 镜像：FAT32，写入 UEFI 启动文件与内核。

ESP 是 UEFI 从其中加载 Linux 的分区，格式 vfat，不需要挂载即可用
mtools（mformat/mcopy）离线写入，所以在任何环境都能跑。

用法：
    scripts/build-esp-image.py --files boot/BOOTAA64.EFI --kernel Image --dtb board.dtb \
        --size-mib 512 --output esp.img --manifest esp-manifest.json
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size-mib", type=int, default=512)
    parser.add_argument("--label", default="SUNUEFI_ESP")
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest")
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

        target = Path(args.output).resolve()
        if target.exists():
            raise EspError(f"output already exists: {target}")

        entries = []
        for spec in args.file:
            if ":" not in spec:
                raise EspError(f"bad --file {spec!r}; expected target-path:source-file")
            guest, source = spec.split(":", 1)
            src = Path(source).resolve()
            if not src.is_file():
                raise EspError(f"source not found: {src}")
            entries.append({"guest": guest.lstrip("/"), "source": str(src), "bytes": src.stat().st_size, "sha256": sha256(src)})

        with target.open("xb") as stream:
            stream.truncate(args.size_mib * MIB)
        # 4096 扇区：与本机 UFS 逻辑扇区一致。
        run(["mkfs.vfat", "-F", "32", "-n", args.label, "-S", "4096", target])

        made_dirs = set()
        for entry in entries:
            parent = str(Path(entry["guest"]).parent)
            if parent not in (".", "") and parent not in made_dirs:
                run(["mmd", "-i", target, "::/" + parent])
                made_dirs.add(parent)
            run(["mcopy", "-i", target, entry["source"], "::/" + entry["guest"]])

        listing = subprocess.run(["mdir", "-i", target, "-/", "-b"], check=True, capture_output=True).stdout.decode()
        result = {
            "status": "ESP_BUILT",
            "output": str(target),
            "size_mib": args.size_mib,
            "label": args.label,
            "sector_size": 4096,
            "files": entries,
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
