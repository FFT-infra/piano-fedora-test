#!/usr/bin/env python3
"""离线检查 initramfs 构建器的参数校验、拒绝路径与归档结构。

用 fixture 走一遍真实构建：静态 BusyBox 用宿主机的（要求 AArch64 且静态），
内核模块是占位文件。真正能启动要等设备，这里只证明归档正确、依赖闭包完整、
拒绝路径生效。
"""

import gzip
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from kernel_fixture import make_kernel

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "build-initramfs.py"
BOOTSTRAP = ROOT / "initramfs" / "f2fs-disk-bootstrap"

RELEASE = "7.2.9-fixture"


def run(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=ROOT,
    )


def static_aarch64_busybox():
    """找一个静态 AArch64 BusyBox；宿主不是该架构时返回 None。"""
    candidate = shutil.which("busybox")
    if not candidate:
        return None
    header = Path(candidate).read_bytes()[:64]
    if len(header) < 64 or header[:5] != b"\x7fELF\x02":
        return None
    if struct.unpack_from("<H", header, 18)[0] != 183:
        return None
    program = subprocess.run(["readelf", "-l", candidate],
                             capture_output=True, text=True).stdout
    if "Requesting program interpreter" in program:
        return None
    return Path(candidate)


def build_fixture(tmp, busybox):
    rootfs = tmp / "rootfs"
    (rootfs / "usr" / "bin").mkdir(parents=True)
    (rootfs / "usr/lib64").mkdir()
    (rootfs / "usr/lib").mkdir()
    (rootfs / "lib64").symlink_to("usr/lib64")
    (rootfs / "lib").symlink_to("usr/lib")
    shutil.copy2(busybox, rootfs / "usr" / "bin" / "busybox")
    # blkid 走动态链接；本机没有时跳过依赖闭包部分。
    blkid = shutil.which("blkid")
    if blkid:
        shutil.copy2(blkid, rootfs / "usr" / "bin" / "blkid")
        for library in ("libblkid.so.1", "libc.so.6"):
            for directory in ("/usr/lib64", "/lib64", "/usr/lib"):
                source = Path(directory) / library
                if source.exists():
                    shutil.copy2(source.resolve(), rootfs / "lib64" / library)
                    break
        loader = Path("/usr/lib/ld-linux-aarch64.so.1")
        if loader.exists():
            shutil.copy2(loader, rootfs / "lib" / "ld-linux-aarch64.so.1")

    rescue = rootfs / "usr/local/sbin/piano-debug-bootstrap"
    rescue.parent.mkdir(parents=True)
    rescue.write_text("#!/bin/sh\nexit 0\n")
    rescue.chmod(0o755)
    kernel = tmp / "kernel"
    modules = kernel / "modules" / "lib" / "modules" / RELEASE
    (modules / "kernel" / "drivers" / "iommu").mkdir(parents=True)
    (modules / "kernel" / "drivers" / "ufs").mkdir(parents=True)
    (modules / "kernel" / "drivers" / "pinctrl").mkdir(parents=True)
    (modules / "kernel" / "drivers" / "mailbox").mkdir(parents=True)
    (modules / "kernel" / "drivers" / "phy" / "qualcomm").mkdir(parents=True)
    for relative in (
        "kernel/drivers/iommu/arm-smmu.ko",
        "kernel/drivers/ufs/ufs-qcom.ko",
        "kernel/drivers/pinctrl/pinctrl-sm8750.ko",
        "kernel/drivers/mailbox/qcom-cpucp-mbox.ko",
        "kernel/drivers/phy/qualcomm/phy-qcom-qmp-ufs.ko",
    ):
        (modules / relative).write_bytes(b"placeholder-module\n")
    (modules / "modules.dep").write_text(
        "kernel/drivers/iommu/arm-smmu.ko:\n"
        "kernel/drivers/pinctrl/pinctrl-sm8750.ko: kernel/drivers/iommu/arm-smmu.ko\n"
        "kernel/drivers/mailbox/qcom-cpucp-mbox.ko: kernel/drivers/iommu/arm-smmu.ko\n"
        "kernel/drivers/phy/qualcomm/phy-qcom-qmp-ufs.ko: kernel/drivers/iommu/arm-smmu.ko\n"
        "kernel/drivers/ufs/ufs-qcom.ko: kernel/drivers/iommu/arm-smmu.ko"
        " kernel/drivers/phy/qualcomm/phy-qcom-qmp-ufs.ko\n"
    )
    (modules / "modules.builtin").write_text("kernel/drivers/soc/qcom/qcom-pmic-glink.ko\n")
    (modules / "modules.softdep").write_text("")
    (kernel / "manifest.json").write_text(json.dumps({
        "kernel_release": RELEASE,
        "source_commit": "352508459733d3e6d349ea5581a8dd2fd8bb4180",
    }))
    # 覆盖前面的占位 metadata 为可验证的封存格式，字节仍明确是测试 fixture。
    make_kernel(kernel, RELEASE)
    return rootfs, kernel


def read_newc(archive):
    """从 gzip 的 cpio 里取出条目名与内容。"""
    raw = gzip.decompress(Path(archive).read_bytes())
    records, offset = {}, 0
    while offset + 110 <= len(raw):
        if raw[offset:offset + 6] != b"070701":
            raise ValueError(f"bad magic at {offset}")
        fields = [int(raw[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        size, name_size = fields[6], fields[11]
        name_start = offset + 110
        name = raw[name_start:name_start + name_size - 1].decode()
        data_start = name_start + name_size
        data_start += -data_start % 4
        data = raw[data_start:data_start + size]
        if name == "TRAILER!!!":
            break
        records[name] = data
        offset = data_start + size
        offset += -offset % 4
    return records


def main():
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    # 引导脚本必须存在且语法正确。
    check(BOOTSTRAP.is_file(), "bootstrap script must exist")
    if BOOTSTRAP.is_file():
        syntax = subprocess.run(["sh", "-n", str(BOOTSTRAP)], capture_output=True, text=True)
        check(syntax.returncode == 0, f"bootstrap has a shell syntax error: {syntax.stderr.strip()}")
        text = BOOTSTRAP.read_text()
        # 这是 F2FS 版本：类型检查、挂载类型都要是 f2fs。
        # 注释里会提到 noload 说明为什么不用它，所以只看真正的挂载调用。
        mounts = [line for line in text.splitlines()
                  if line.lstrip().startswith("mount ") and "-t" in line]
        check(mounts, "bootstrap should mount the root filesystem")
        check(all("-t f2fs" in line for line in mounts),
              f"every root mount must be f2fs: {mounts}")
        check(not any("noload" in line for line in mounts),
              "F2FS has no noload option")
        check(any('= f2fs' in line for line in text.splitlines() if "TYPE" in line),
              "bootstrap must require the f2fs filesystem type")
        # 注释里会解释为什么不再用 ext4，所以只检查可执行语句。
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        check("ext4" not in code, "bootstrap must not depend on ext4")

    # 参数缺失：拒绝。
    check(run("--rootfs", "/nonexistent", "--kernel", "/nonexistent",
              "--output", "/nonexistent").returncode == 2,
          "missing inputs should be rejected")

    busybox = static_aarch64_busybox()
    if busybox is None:
        # 宿主不是 AArch64 或没有静态 BusyBox：明确记录跳过，不算通过。
        if failures:
            for item in failures:
                print(f"FAIL: {item}", file=sys.stderr)
            return 1
        print("SKIP: no static AArch64 busybox on this host; "
              "argument checks only (not a pass)")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rootfs, kernel = build_fixture(tmp, busybox)
        output = tmp / "out"

        # 输出已存在：拒绝。
        output.mkdir()
        check(run("--rootfs", str(rootfs), "--kernel", str(kernel),
                  "--output", str(output)).returncode == 2,
              "existing output should be rejected")
        output.rmdir()

        result = run("--rootfs", str(rootfs), "--kernel", str(kernel),
                     "--output", str(output))
        if result.returncode != 0:
            check(False, f"build failed: {result.stderr.strip()}")
        else:
            manifest = json.loads((output / "manifest.json").read_text())
            check(manifest["root_fstype"] == "f2fs", "manifest must record f2fs")
            check(manifest["root_policy"] == "LABEL=PIANOROOT", "root policy must be the piano label")
            check(manifest["device_tested"] is False, "must not claim device testing")
            check(
                manifest["modules"] == [
                    "arm_smmu", "pinctrl_sm8750", "qcom_cpucp_mbox",
                    "phy_qcom_qmp_ufs", "ufs_qcom",
                ],
                f"module closure order wrong: {manifest['modules']}",
            )

            records = read_newc(output / "initramfs.cpio.gz")
            for required in ("init", "pianoinit", "bin/busybox", "bin/sh",
                             "bin/mount", "bin/switch_root", "bin/insmod",
                             "etc/piano/root-label", "etc/piano/root-partname",
                             "etc/piano/kernel-release", "etc/piano/modules-load-order",
                             "dev/console", "dev/null"):
                check(required in records, f"missing from initramfs: {required}")
            check(records.get("etc/piano/root-label") == b"PIANOROOT\n",
                  "root label content wrong")
            check(records.get("etc/piano/root-partname") == b"sunuefi_root\n",
                  "root partname content wrong")
            # 引导脚本必须与仓库里的那份逐字节一致。
            check(records.get("pianoinit") == BOOTSTRAP.read_bytes(),
                  "archived bootstrap differs from the repository copy")
            # 动态依赖必须一起进归档。
            if shutil.which("blkid"):
                check("usr/bin/blkid" in records, "blkid missing from initramfs")
                check("lib64/libblkid.so.1" in records,
                      "loader must find libblkid at /lib64 even with a merged-usr root")
                check(any(name.startswith("lib64/") or name.startswith("lib/")
                          for name in records),
                      "ELF dependency closure missing")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print("OK: initramfs builder, closure, archive structure and reject paths behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
