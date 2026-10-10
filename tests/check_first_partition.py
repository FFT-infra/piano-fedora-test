#!/usr/bin/env python3
"""离线检查首次分区计划工具。用真实 GPT 快照做 fixture，不接触设备。"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "plan-first-partition.py"

# 2026-10-09 从本机 piano 只读捕获的 sgdisk --print 尾部。
REAL_GPT = """Disk /dev/block/sda: 249518080 sectors, 951.8 GiB
Sector size (logical/physical): 4096/4096 bytes
Disk identifier (GUID): D55FA102-1ED9-4154-B3A7-3E84CD916CB5
Partition table holds up to 64 entries
First usable sector is 6, last usable sector is 249518074
Total free space is 0 sectors (0 bytes)

Number  Start (sector)    End (sector)  Size       Code  Name
   1               6               7   8.0 KiB     FFFF  switch
  32          191488          224255   128.0 MiB   A039  rescue
  33          224256         3632127   13.0 GiB    FFFF  super
  34         3632128       249518074   938.0 GiB   A03A  userdata
"""


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
        dump = tmp / "gpt.txt"
        dump.write_text(REAL_GPT, encoding="utf-8")

        # inspect-only：如实报告无空位。
        result = run("--dump", str(dump), "--inspect-only")
        check(result.returncode == 0, "inspect should succeed")
        if result.returncode == 0:
            data = json.loads(result.stdout)
            check(data["free_sectors"] == 0, "real dump should show zero free sectors")
            check(data["device_writes"] is False, "inspect must not write")

        # 正常计划：ESP + root 从 userdata 尾部切出。
        result = run("--dump", str(dump), "--esp-mib", "512", "--root-mib", "16384")
        check(result.returncode == 0, f"plan should succeed, got {result.stderr}")
        if result.returncode == 0:
            data = json.loads(result.stdout)
            check(data["mode"] == "tail-split", "mode should be tail-split")
            check(data["device_writes"] is False, "plan must not write")
            changes = {c["name"]: c for c in data["changes"]}
            check("userdata" in changes, "plan should shrink userdata")
            check(changes["userdata"]["old_last"] == 249518074, "userdata old_last should match dump")
            check("sunuefi_root" in changes, "plan should add sunuefi_root")
            check("sunuefi_esp" in changes, "plan should add sunuefi_esp")
            # 起点按 1 MiB 对齐会把起点往前推，实际容量只会大于等于请求值。
            check(changes["sunuefi_root"]["size_mib"] >= 16384, "root size should be at least the request")
            check(changes["sunuefi_root"]["size_mib"] <= 16384 + 1, "root rounding should stay within 1 MiB")
            check(changes["sunuefi_esp"]["size_mib"] >= 512, "esp size should be at least the request")
            check(changes["sunuefi_esp"]["size_mib"] <= 512 + 1, "esp rounding should stay within 1 MiB")
            # 尾部必须被完整占用。
            check(changes["sunuefi_esp"]["last"] == 249518074, "esp should end at the disk tail")
            # 新分区不能重叠。
            check(changes["userdata"]["new_last"] < changes["sunuefi_root"]["first"], "userdata must not overlap root")
            check(changes["sunuefi_root"]["last"] < changes["sunuefi_esp"]["first"], "root must not overlap esp")

        result = run("--dump", str(dump), "--equal-split")
        check(result.returncode == 0, f"equal split should succeed: {result.stderr}")
        if result.returncode == 0:
            data = json.loads(result.stdout)
            check(data["allocation"] == "equal-userdata-after-esp", "equal split policy must be explicit")
            check(data["capacity"]["linux_root_mib"] == 479990, "real GPT half capacity must not be capped at 256 GiB")
            check(data["capacity"]["android_userdata_mib"] == 479989, "Android must retain its aligned half")
            check(data["capacity"]["equal_split_difference_bytes"] <= 2 * 1024**2, "rounding must stay within two alignment units")
            check(data["android_data_preserved"] is False, "destructive userdata handling must be explicit")
            check(all(c["partition"] <= 64 for c in data["changes"]), "new entries must respect actual GPT capacity")
            check(len(data["baseline_sha256"]) == 64, "plan must bind its source geometry")

        large = run("--dump", str(dump), "--root-mib", "480000")
        check(large.returncode == 0, "explicit root above 256 GiB must be accepted if it fits")
        for replacement, message in (
            (REAL_GPT.replace("64 entries", "4096 entries"), "GPT tables overlap usable area"),
            (REAL_GPT.replace("64 entries", "4 entries").replace("  32 ", "   2 ")
                     .replace("  33 ", "   3 ").replace("  34 ", "   4 "), "no free GPT slots"),
            (REAL_GPT.replace("34         3632128", "33         3632128"), "duplicate partition index"),
            (REAL_GPT.replace("34         3632128", "34         3632129"), "unaligned userdata start"),
            (REAL_GPT.replace("249518074   938.0", "249518070   938.0"), "userdata must fill tail"),
            (REAL_GPT.replace("  34 ", "  65 "), "entry index exceeds table"),
        ):
            bad = tmp / "invalid.txt"
            bad.write_text(replacement, encoding="utf-8")
            check(run("--dump", str(bad), "--equal-split").returncode == 2, message + " must be rejected")

        unnamed = tmp / "unnamed.txt"
        unnamed.write_text(REAL_GPT +
                           "   2               8              15   32.0 KiB    FFFF\n", encoding="utf-8")
        result = run("--dump", str(unnamed), "--equal-split")
        check(result.returncode == 0, "unnamed occupied GPT entries must remain visible")
        if result.returncode == 0:
            data = json.loads(result.stdout)
            check(any(p["number"] == 2 and p["name"] == "" for p in data["original_partitions"]), "unnamed entry was omitted")
            check(all(c["partition"] != 2 for c in data["changes"]), "unnamed occupied entry was reused")

        # userdata 太小、放不下 ESP+root：拒绝。
        # 用一个小盘做 fixture，避免依赖真实盘的容量。
        small = tmp / "small.txt"
        small.write_text(
            REAL_GPT.replace("249518080", "200000")
                    .replace("249518074", "199999")
                    .replace("3632128       249518074", "190000       199999"),
            encoding="utf-8",
        )
        check(
            run("--dump", str(small), "--root-mib", "262144").returncode == 2,
            "root larger than available space should be rejected",
        )

        # ESP 越界：拒绝。
        check(
            run("--dump", str(dump), "--esp-mib", "4096").returncode == 2,
            "oversized esp should be rejected",
        )

        # 扇区大小不符：拒绝。
        bad = tmp / "bad.txt"
        bad.write_text(REAL_GPT.replace("4096/4096", "512/4096"), encoding="utf-8")
        check(run("--dump", str(bad)).returncode == 2, "512-byte sectors should be rejected")

        # 没有 userdata：拒绝。
        nodata = tmp / "nodata.txt"
        nodata.write_text("\n".join(l for l in REAL_GPT.splitlines() if "userdata" not in l), encoding="utf-8")
        check(run("--dump", str(nodata)).returncode == 2, "missing userdata should be rejected")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print("OK: first-partition plan and reject paths behave as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
