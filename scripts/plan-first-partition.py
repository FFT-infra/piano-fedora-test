#!/usr/bin/env python3
"""生成首次分区计划：从满盘的 userdata 尾部切出 ESP 与 Linux root。

上游 install_piano.py 对首次分区返回 NEW_INSTALL_NOT_READY，理由是缺少
经验证的 Android 缩容 helper 与 GPT 写传输。本工具只做**计划**：读入真实
GPT，算出新的分区布局，并把结果写成 JSON 供复核。它不写设备。

约束来自本机实测：
- 逻辑/物理扇区都是 4096 字节
- userdata 占满盘尾，全盘无空位
- userdata 是加密 dm 映射，无损缩容的前提不成立

因此计划分两种：
- tail-split：保持 userdata 起点不变，只缩短它，在尾部放 ESP + root。
  这会破坏 userdata 里的现有数据，是初装路径。
- inspect-only：只报告现状与可行性，不改任何东西。

用法：
    scripts/plan-first-partition.py --dump GPT.txt --esp-mib 512 --root-mib 16384
    scripts/plan-first-partition.py --dump GPT.txt --inspect-only
"""

import argparse
import json
import re
import sys
from pathlib import Path

SECTOR = 4096
MIB = 1024 * 1024
SECTORS_PER_MIB = MIB // SECTOR
# 新分区起点按 1 MiB 对齐，这是分区工具的常规做法。
ALIGN_SECTORS = MIB // SECTOR


def align_down(value, alignment):
    return value - (value % alignment)


class PlanError(Exception):
    """输入不满足要求，属于可预期失败。"""


def parse_sgdisk(text):
    """解析 sgdisk --print 的输出。返回磁盘信息与分区列表。"""
    disk = {}
    m = re.search(r"Disk .*?: ([\d,]+) sectors", text)
    if m:
        disk["sectors"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Sector size \(logical/physical\): (\d+)/(\d+) bytes", text)
    if m:
        disk["logical_sector"] = int(m.group(1))
        disk["physical_sector"] = int(m.group(2))
    m = re.search(r"First usable sector is (\d+), last usable sector is (\d+)", text)
    if m:
        disk["first_usable"] = int(m.group(1))
        disk["last_usable"] = int(m.group(2))

    parts = []
    for line in text.splitlines():
        row = re.match(
            r"\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+ \w+)\s+([0-9A-F]{4})\s+(\S+)", line
        )
        if row:
            parts.append({
                "number": int(row.group(1)),
                "first": int(row.group(2)),
                "last": int(row.group(3)),
                "size_text": row.group(4),
                "code": row.group(5),
                "name": row.group(6),
            })
    if not parts:
        raise PlanError("no partitions parsed; is this sgdisk --print output?")
    return disk, parts


def find_userdata(parts):
    for part in parts:
        if part["name"] == "userdata":
            return part
    raise PlanError("no userdata partition found")


def build_plan(disk, parts, esp_mib, root_mib):
    if disk.get("logical_sector") != SECTOR:
        raise PlanError(
            f"this plan assumes {SECTOR}-byte logical sectors, got {disk.get('logical_sector')}"
        )

    userdata = find_userdata(parts)
    esp_want = esp_mib * SECTORS_PER_MIB
    root_want = root_mib * SECTORS_PER_MIB
    available = userdata["last"] - userdata["first"] + 1
    if esp_want + root_want >= available:
        raise PlanError(
            f"need {esp_want + root_want} sectors for ESP+root, userdata only has {available}"
        )

    # 从盘尾往前排：ESP 占尾，root 紧随其后，两者起点各自按 1 MiB 对齐。
    # 对齐把起点往前推，所以实际容量只会大于等于请求值。
    # userdata 起点不动，只把末尾缩短到 root 起点之前。
    esp_last = userdata["last"]
    esp_first = align_down(esp_last - esp_want + 1, ALIGN_SECTORS)
    root_last = esp_first - 1
    root_first = align_down(root_last - root_want + 1, ALIGN_SECTORS)
    new_userdata_last = root_first - 1

    if new_userdata_last <= userdata["first"]:
        raise PlanError("userdata would shrink below its start; request too large")
    for label, value in (("root_first", root_first), ("esp_first", esp_first)):
        if value % ALIGN_SECTORS:
            raise PlanError(f"{label}={value} is not {ALIGN_SECTORS}-sector aligned")
    if esp_last != userdata["last"]:
        raise PlanError("tail split did not consume the full tail")

    root_sectors = root_last - root_first + 1
    esp_sectors = esp_last - esp_first + 1

    used_numbers = {p["number"] for p in parts}
    free_numbers = [n for n in range(1, 129) if n not in used_numbers]
    if len(free_numbers) < 2:
        raise PlanError("fewer than two free GPT entries for ESP and root")

    return {
        "status": "PLAN",
        "mode": "tail-split",
        "disk": disk,
        "changes": [
            {
                "partition": userdata["number"],
                "name": "userdata",
                "old_last": userdata["last"],
                "new_last": new_userdata_last,
                "note": "起点不变，只缩短尾部；分区内现有数据不再可用",
            },
            {
                "partition": free_numbers[0],
                "name": "sunuefi_root",
                "first": root_first,
                "last": root_last,
                "size_sectors": root_sectors,
                "size_mib": root_sectors // SECTORS_PER_MIB,
                "type": "linux",
                "fs": "f2fs",
                "label": "PIANOROOT",
            },
            {
                "partition": free_numbers[1],
                "name": "sunuefi_esp",
                "first": esp_first,
                "last": esp_last,
                "size_mib": esp_mib,
                "type": "efi",
                "fs": "vfat",
                "label": "SUNUEFI_ESP",
            },
        ],
        "warnings": [
            "这会破坏 userdata 内的现有数据，是初装路径，不是无损缩容。",
            "本机 userdata 是加密 dm 映射，无损缩容的前提不成立。",
            "计划只是计算；写 GPT 是独立步骤，需要先有可恢复备份。",
        ],
        "device_writes": False,
    }


def inspect(disk, parts):
    userdata = find_userdata(parts)
    total = disk.get("sectors", 0)
    return {
        "status": "INSPECT",
        "disk": disk,
        "userdata": userdata,
        "free_sectors": 0 if userdata["last"] >= disk.get("last_usable", 0) else disk["last_usable"] - userdata["last"],
        "note": "userdata 占满尾部，全盘无空位" if userdata["last"] >= disk.get("last_usable", 0) else "尾部有空位",
        "device_writes": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dump", required=True, help="sgdisk --print 的输出文件")
    parser.add_argument("--esp-mib", type=int, default=512)
    parser.add_argument("--root-mib", type=int, default=16384)
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--output", help="把计划写入这个 JSON")
    args = parser.parse_args(argv)

    try:
        text = Path(args.dump).read_text(encoding="utf-8")
        disk, parts = parse_sgdisk(text)

        if args.inspect_only:
            result = inspect(disk, parts)
        else:
            if not 64 <= args.esp_mib <= 2048:
                raise PlanError(f"esp size must be 64..2048 MiB, got {args.esp_mib}")
            if not 1024 <= args.root_mib <= 262144:
                raise PlanError(f"root size must be 1024..262144 MiB, got {args.root_mib}")
            result = build_plan(disk, parts, args.esp_mib, args.root_mib)

        if args.output:
            Path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except PlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
