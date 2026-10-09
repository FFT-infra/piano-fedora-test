#!/usr/bin/env python3
"""为 Fedora + F2FS 根分区构建 initramfs。

上游 `tools/build_release_rootfs.py` 的 bootstrap 绑定 Debian：
`dpkg-query` 查 busybox-static 包、库路径写死 `/lib/aarch64-linux-gnu/`、
`release-disk-bootstrap` 要求 `TYPE=ext4` 且用 `ro,noload` 挂载。Fedora 的
库在 `/lib64/`，也没有 dpkg，所以那份实现不能直接用。

这里复用上游的思路（静态 BusyBox + 依赖闭包 + 模块闭包 + 两段式挂载），
但不依赖发行版包管理器，也不 fork 上游。

引导脚本用本仓库的 `initramfs/f2fs-disk-bootstrap`：它保留上游"两次身份
校验、先只读后读写"的结构，把类型检查与挂载改成 F2FS 语义。

用法：
    scripts/build-initramfs.py --rootfs DIR --kernel DIR --output DIR
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "initramfs" / "f2fs-disk-bootstrap"

MIB = 1024 * 1024

# BusyBox 从 Fedora 的 busybox 包来，静态链接，不需要额外库。
# 这些 applet 是引导脚本实际用到的，多一个少一个都会在启动时才暴露。
APPLETS = (
    "sh", "cat", "mkdir", "mount", "mountpoint", "chmod", "uname", "chroot",
    "switch_root", "sleep", "insmod", "umount", "grep", "tr", "id", "awk",
    "readlink", "rm", "rmdir", "ln",
)

# 早期硬件准备需要按序加载的模块，与上游 disk bootstrap 的种子一致。
SEEDS = ("arm_smmu", "pinctrl_sm8750", "qcom_cpucp_mbox",
         "phy_qcom_qmp_ufs", "ufs_qcom")

# Fedora 把动态库放在 /lib64，解释器在 /lib。这是与 Debian 的关键差异。
FEDORA_LIBDIRS = ("/lib64", "/usr/lib64", "/lib")


class InitramfsError(Exception):
    """输入或环境不满足，属于可预期失败。"""


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def guest_resolve(root, name):
    """在 guest 根内解析绝对符号链接，不跳出到宿主。"""
    root = Path(root).resolve()
    pending = name.strip("/").split("/")
    parts = []
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise InitramfsError("guest link escapes root")
            parts.pop()
            continue
        candidate = root.joinpath(*parts, part)
        if candidate.is_symlink():
            hops += 1
            if hops > 40:
                raise InitramfsError("guest symlink cycle")
            target = str(candidate.readlink())
            if target.startswith("/"):
                parts = []
            pending = target.split("/") + pending
        else:
            parts.append(part)
    return root.joinpath(*parts)


def elf_dependencies(path):
    """读 ELF 的 NEEDED 与 interpreter，并确认是 AArch64。"""
    header = Path(path).read_bytes()[:64]
    if len(header) < 64 or header[:5] != b"\x7fELF\x02" or struct.unpack_from("<H", header, 18)[0] != 183:
        raise InitramfsError(f"expected ELF64 AArch64: {path}")
    dynamic = subprocess.check_output(["readelf", "-d", str(path)], text=True)
    program = subprocess.check_output(["readelf", "-l", str(path)], text=True)
    needed = re.findall(r"\(NEEDED\).*\[([^\]]+)\]", dynamic)
    interpreter = re.findall(r"Requesting program interpreter: ([^\]]+)\]", program)
    return needed, interpreter


def resolve_library(root, soname):
    """在 Fedora 的库目录里找 soname。

    Debian 用一个固定目录 `/lib/aarch64-linux-gnu/`，Fedora 分 `/lib64` 与
    `/usr/lib64`。这里按顺序找，找不到就报错，不猜。
    """
    for directory in FEDORA_LIBDIRS:
        candidate = guest_resolve(root, f"{directory}/{soname}")
        if candidate.is_file():
            return candidate
    raise InitramfsError(f"missing shared library: {soname}")


def runtime_files(root, programs):
    """算出一组程序的完整 ELF 依赖闭包。"""
    pending = list(programs)
    files = {}
    while pending:
        name = pending.pop(0)
        key = name.lstrip("/")
        if key in files:
            continue
        source = guest_resolve(root, name)
        if not source.is_file():
            raise InitramfsError(f"missing runtime dependency: {name}")
        needed, interpreters = elf_dependencies(source)
        files[key] = source
        pending += interpreters
        for soname in needed:
            if "/" in soname:
                raise InitramfsError(f"unexpected ELF dependency path: {soname}")
            resolved = resolve_library(root, soname)
            pending.append("/" + str(resolved.relative_to(Path(root).resolve())))
    return files


def module_name(path):
    return Path(path).name.removesuffix(".ko").replace("-", "_")


def module_closure(directory):
    """按依赖顺序展开早期模块闭包。

    与上游 `build_piano_disk_bootstrap.module_closure` 同构：按 seeds 深度优先，
    依赖先于被依赖者。
    """
    deps = dict(
        line.split(":", 1)
        for line in (directory / "modules.dep").read_text().splitlines()
        if line.strip()
    )
    names = {module_name(path): path for path in deps}
    builtin = {
        module_name(path)
        for path in (directory / "modules.builtin").read_text().splitlines()
        if path.strip()
    }
    softdeps = {}
    softdep_file = directory / "modules.softdep"
    if softdep_file.is_file():
        for line in softdep_file.read_text().splitlines():
            words = line.split()
            if words and words[0] == "softdep":
                softdeps[words[1].replace("-", "_")] = [
                    word.replace("-", "_") for word in words[2:]
                    if word not in ("pre:", "post:")
                ]

    ordered, seen, active, used_builtin = [], set(), set(), set()

    def visit(name):
        if name in builtin:
            used_builtin.add(name)
            return
        if name in seen:
            return
        if name in active or name not in names:
            raise InitramfsError(f"missing or cyclic module dependency: {name}")
        active.add(name)
        for dependency in softdeps.get(name, []):
            visit(dependency)
        for dependency in deps[names[name]].split():
            visit(module_name(dependency))
        active.remove(name)
        seen.add(name)
        ordered.append(names[name])

    for seed in SEEDS:
        visit(seed)
    return ordered, sorted(used_builtin)


def make_newc(entries):
    """生成 newc 归档。与上游 make_kernel_initramfs 的协议一致。"""
    archive = bytearray()
    seen = set()
    for inode, record in enumerate(entries + [{"name": "TRAILER!!!", "mode": 0, "data": b""}], 1):
        name, mode, data = record["name"], record["mode"], record.get("data", b"")
        if (not name or name.startswith("/") or "\0" in name or ".." in name.split("/")):
            raise InitramfsError(f"invalid archive path: {name!r}")
        if name in seen:
            raise InitramfsError(f"duplicate archive path: {name}")
        seen.add(name)
        if stat.S_ISLNK(mode):
            target = data.decode()
            if not target or target.startswith("/") or ".." in target.split("/"):
                raise InitramfsError(f"invalid symlink target: {target!r}")
            resolved = (Path(name).parent / target).as_posix()
            if resolved not in {item["name"] for item in entries}:
                raise InitramfsError(f"symlink target not supplied: {name}")
        raw_name = name.encode() + b"\0"
        fields = [inode, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0, len(data),
                  0, 0, record.get("major", 0), record.get("minor", 0), len(raw_name), 0]
        if any(not 0 <= value <= 0xFFFFFFFF for value in fields):
            raise InitramfsError("newc field exceeds protocol limit")
        archive.extend(b"070701" + b"".join(f"{value:08x}".encode() for value in fields))
        archive.extend(raw_name)
        archive.extend(b"\0" * (-len(archive) % 4))
        archive.extend(data)
        archive.extend(b"\0" * (-len(archive) % 4))
    archive.extend(b"\0" * (-len(archive) % 512))
    return bytes(archive)


def inspect_newc(archive):
    """读回 newc，确认写入的内容与预期一致。"""
    records, seen, offset = [], set(), 0
    while offset + 110 <= len(archive):
        magic = archive[offset:offset + 6]
        if magic != b"070701":
            raise InitramfsError(f"bad newc magic at {offset}: {magic!r}")
        fields = [int(archive[offset + 6 + index * 8:offset + 14 + index * 8], 16)
                  for index in range(13)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        name_start = offset + 110
        name = archive[name_start:name_start + name_size - 1].decode()
        data_start = name_start + name_size
        data_start += -data_start % 4
        data = archive[data_start:data_start + size]
        if name == "TRAILER!!!":
            return records
        if name in seen:
            raise InitramfsError(f"duplicate newc path: {name}")
        seen.add(name)
        records.append({"name": name, "mode": mode, "size": size, "data": data})
        offset = data_start + size
        offset += -offset % 4
    raise InitramfsError("newc archive has no trailer")


def build(rootfs, kernel, output, root_label, root_partname, release_override=None):
    rootfs, kernel, output = Path(rootfs).resolve(), Path(kernel).resolve(), Path(output).resolve()
    if output.exists():
        raise InitramfsError(f"output already exists: {output}")
    if not rootfs.is_dir():
        raise InitramfsError(f"rootfs is not a directory: {rootfs}")
    if not BOOTSTRAP.is_file():
        raise InitramfsError(f"missing bootstrap: {BOOTSTRAP}")

    manifest_path = kernel / "manifest.json"
    if not manifest_path.is_file():
        raise InitramfsError(f"kernel manifest missing: {manifest_path}")
    kernel_manifest = json.loads(manifest_path.read_text())
    release = release_override or kernel_manifest["kernel_release"]
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", release):
        raise InitramfsError(f"invalid kernel release: {release!r}")

    # BusyBox 必须存在且是静态 AArch64。
    busybox = guest_resolve(rootfs, "/usr/bin/busybox")
    if not busybox.is_file():
        raise InitramfsError(
            "missing /usr/bin/busybox in the rootfs; add busybox to the Fedora profile"
        )
    header = busybox.read_bytes()[:64]
    if header[:5] != b"\x7fELF\x02" or struct.unpack_from("<H", header, 18)[0] != 183:
        raise InitramfsError("busybox is not an ELF64 AArch64 binary")
    if b"Requesting program interpreter" in subprocess.check_output(
        ["readelf", "-l", str(busybox)]
    ):
        raise InitramfsError("busybox must be statically linked")

    applets = set(
        subprocess.check_output([str(busybox), "--list"], text=True).splitlines()
    )
    missing = set(APPLETS) - applets
    if missing:
        raise InitramfsError("busybox is missing applets: " + ", ".join(sorted(missing)))

    # blkid 走动态链接，需要它的完整依赖闭包。
    files = runtime_files(rootfs, ("/usr/bin/blkid",))

    module_dir = kernel / "modules" / "lib" / "modules" / release
    if not module_dir.is_dir():
        raise InitramfsError(f"missing kernel modules: {module_dir}")
    ordered, builtin = module_closure(module_dir)
    if not ordered:
        raise InitramfsError("empty module closure")

    rows = {}
    # blkid 的动态依赖闭包必须进归档，否则挂载阶段找不到库。
    rows.update(files)
    for path in ordered:
        rows[f"lib/modules/{release}/{path}"] = module_dir / path
    rows["bin/busybox"] = busybox
    rows["pianoinit"] = BOOTSTRAP
    rows["init"] = BOOTSTRAP
    rows[f"lib/modules/{release}/modules.builtin"] = module_dir / "modules.builtin"

    generated = {
        "etc/piano/root-label": root_label + "\n",
        "etc/piano/root-partname": root_partname + "\n",
        "etc/piano/kernel-release": release + "\n",
        "etc/piano/modules-load-order": "".join(
            f"{module_name(path)} /lib/modules/{release}/{path}\n" for path in ordered
        ),
    }

    directories = {"dev", "proc", "sys", "run", "tmp", "sysroot"}
    for name in list(rows) + list(generated):
        directories.update(
            parent.as_posix()
            for parent in Path(name).parents
            if parent.as_posix() != "."
        )

    entries = [
        {"name": name, "mode": stat.S_IFDIR | (0o1777 if name == "tmp" else 0o755)}
        for name in sorted(directories, key=lambda value: (value.count("/"), value))
    ]
    entries += [
        {"name": "dev/console", "mode": stat.S_IFCHR | 0o600, "major": 5, "minor": 1},
        {"name": "dev/null", "mode": stat.S_IFCHR | 0o666, "major": 1, "minor": 3},
    ]

    sources = {name: path for name, path in rows.items()}
    digests = {name: sha256(path) for name, path in sources.items()}
    entries += [
        {"name": name, "mode": stat.S_IFREG | 0o755, "data": path.read_bytes()}
        for name, path in sorted(sources.items())
    ]
    entries += [
        {"name": name, "mode": stat.S_IFREG | 0o644, "data": data.encode()}
        for name, data in sorted(generated.items())
    ]
    entries += [
        {"name": "bin/" + name, "mode": stat.S_IFLNK | 0o777, "data": b"busybox"}
        for name in APPLETS
    ]

    archive = make_newc(entries)
    # 写回读，确认归档可解析且内容一致。
    parsed = inspect_newc(archive)
    if [row["name"] for row in parsed] != [row["name"] for row in entries]:
        raise InitramfsError("archive round-trip changed its contents")
    for row in parsed:
        if row["name"] in digests and hashlib.sha256(row["data"]).hexdigest() != digests[row["name"]]:
            raise InitramfsError(f"archive round-trip changed {row['name']}")
    # 打包期间源文件不能变。
    for name, path in sources.items():
        if sha256(path) != digests[name]:
            raise InitramfsError(f"source changed while packing: {name}")

    output.mkdir(parents=True)
    target = output / "initramfs.cpio.gz"
    target.write_bytes(gzip.compress(archive, mtime=0))

    result = {
        "status": "INITRAMFS_BUILT_NOT_BOOT_VERIFIED",
        "kernel_release": release,
        "kernel_commit": kernel_manifest.get("source_commit"),
        "root_policy": f"LABEL={root_label}",
        "root_partname": root_partname,
        "root_fstype": "f2fs",
        "initramfs_sha256": sha256(target),
        "initramfs_bytes": target.stat().st_size,
        "uncompressed_bytes": len(archive),
        "entry_count": len(entries),
        "modules": [module_name(path) for path in ordered],
        "modules_builtin_used": builtin,
        "busybox": {"path": "/usr/bin/busybox", "sha256": sha256(busybox), "static": True},
        "bootstrap": {"path": str(BOOTSTRAP.relative_to(ROOT)), "sha256": sha256(BOOTSTRAP)},
        "runtime_libraries": {
            name: digests[name] for name in sorted(files)
        },
        "device_tested": False,
    }
    (output / "manifest.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rootfs", required=True, help="已组装的 Fedora rootfs 目录")
    parser.add_argument("--kernel", required=True, help="内核 artifact 目录（含 manifest.json 与 modules/）")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--root-label", default="PIANOROOT")
    parser.add_argument("--root-partname", default="sunuefi_root")
    parser.add_argument("--release", help="覆盖内核 release（默认取 kernel manifest）")
    args = parser.parse_args(argv)

    try:
        result = build(args.rootfs, args.kernel, args.output,
                       args.root_label, args.root_partname, args.release)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except InitramfsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
