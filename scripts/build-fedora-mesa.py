#!/usr/bin/env python3
"""重建固定 Fedora Mesa 源 RPM，加入锁定的 piano A830v1 补丁。"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import urllib.request
from pathlib import Path

from piano_artifacts import sha256

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://kojipkgs.fedoraproject.org/packages/mesa/26.2.3/1.fc44/src/mesa-26.2.3-1.fc44.src.rpm"
SOURCE_SHA256 = "f82d91e5f2b34665399e60664907d7e3d318ae4712b5fa60334e44b4ddce833b"
SPEC_SHA256 = "a03631907c257a72a7a1dd169e4a097a2aaa37cedd6dc175b579a1c972de3212"
TARBALL_SHA256 = "1628058a8d2c0615975de5a15ab7bbb9638c50000b5bed9456ff423ea034a81f"
PATCH = "0001-freedreno-Add-chip-id-support-for-A830v1.patch"
PATCH_SHA256 = "01bc8e6b35019f7d6b84bc2d919a60686ffb1742d4df04467a89d3aac22a7380"
PACKAGES = {"mesa-filesystem", "mesa-libGL", "mesa-libEGL", "mesa-libgbm",
            "mesa-dri-drivers", "mesa-vulkan-drivers"}
CHIP_ID = 0xffff44050001


def run(*command):
    subprocess.run([str(x) for x in command], check=True)


def query(*command):
    return subprocess.check_output([str(x) for x in command], text=True).strip()


def install_build_dependencies(spec, srpm, top):
    # SRPM 头的条件依赖在其打包机上已冻结；本机重新展开 spec，
    # 同时保留 SRPM 携带的动态 Rust BuildRequires。
    run("dnf", "-y", "builddep", "--define", "_topdir " + str(top),
        "--spec", spec, "--srpm", srpm)


def adapt_spec(raw):
    if hashlib.sha256(raw).hexdigest() != SPEC_SHA256:
        raise ValueError("Fedora Mesa spec changed; review the recipe")
    text = raw.decode()
    old = "Release:        %autorelease\n"
    if text.count(old) != 1 or text.count("\n%description\n") != 1:
        raise ValueError("unexpected Fedora Mesa spec layout")
    return text.replace(old, "Release:        %autorelease -e piano1\n").replace(
        "\n%description\n", "\nPatch999:       piano-a830v1.patch\n\n%description\n", 1)


def driver_proof(root):
    proof = {}
    for name in ("libgallium-26.2.3.so", "libvulkan_freedreno.so"):
        relative = "usr/lib64/" + name
        path = root / relative
        data = path.read_bytes()
        if (data[:5] != b"\x7fELF\x02" or struct.unpack_from("<H", data, 18)[0] != 183
                or struct.pack("<Q", CHIP_ID) not in data or b"Adreno (TM) 830v1\0" not in data):
            raise ValueError("compiled Mesa lacks the AArch64 A830v1 device table: " + name)
        proof[relative] = sha256(path)
    return proof


def build(sun, output):
    if platform.machine() not in ("aarch64", "arm64") or os.geteuid() != 0:
        raise ValueError("native root Fedora builder required")
    if output.exists() or not output.is_relative_to(ROOT / "build"):
        raise ValueError("choose a fresh build-owned Mesa output")
    lock = json.loads((ROOT / "sources.lock.json").read_text())["sources"]["piano-mesa"]
    source = sun / "upstream/piano-mesa-current"
    if query("git", "-C", source, "rev-parse", "HEAD") != lock["commit"] or query(
            "git", "-C", source, "status", "--porcelain"):
        raise ValueError("Mesa patch checkout is not the clean locked source")
    patch = source / "patches" / PATCH
    if sha256(patch) != PATCH_SHA256:
        raise ValueError("piano Mesa patch changed")
    output.mkdir(parents=True)
    srpm = output / "mesa.src.rpm"
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as incoming, srpm.open("xb") as stream:
        shutil.copyfileobj(incoming, stream)
    if sha256(srpm) != SOURCE_SHA256:
        raise ValueError("Fedora Mesa source RPM changed")
    top = output / "rpmbuild"
    for directory in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (top / directory).mkdir(parents=True)
    run("rpm", "-i", "--nodeps", "--define", "_topdir " + str(top), srpm)
    spec = top / "SPECS/mesa.spec"
    effective = adapt_spec(spec.read_bytes())
    if sha256(top / "SOURCES/mesa-26.2.3.tar.xz") != TARBALL_SHA256:
        raise ValueError("Mesa source tarball differs from the official release")
    spec.write_text(effective)
    shutil.copyfile(patch, top / "SOURCES/piano-a830v1.patch")
    # 复用 Fedora 的完整依赖和打包配方，不手列 Mesa 的大量构建依赖。
    install_build_dependencies(spec, srpm, top)
    run("rpmbuild", "-bb", "--define", "_topdir " + str(top),
        "--define", "_smp_build_ncpus 4", "--define", "_smp_mflags -j4",
        "--define", "debug_package %{nil}", spec)
    rpms = output / "rpms"
    rpms.mkdir()
    selected = {}
    for path in (top / "RPMS").rglob("*.rpm"):
        name, version, release, arch = query(
            "rpm", "-qp", "--qf", "%{NAME} %{VERSION} %{RELEASE} %{ARCH}", path).split()
        if name not in PACKAGES:
            continue
        if name in selected or (version, release, arch) != ("26.2.3", "1.piano1.fc44", "aarch64"):
            raise ValueError("unexpected Mesa RPM identity: " + path.name)
        shutil.copyfile(path, rpms / path.name)
        selected[name] = {"file": path.name, "nevra": f"{name}-{version}-{release}.{arch}"}
    if selected.keys() != PACKAGES:
        raise ValueError("Mesa runtime package set is incomplete")
    # 从实际 RPM 安装载荷核对两个驱动，而非把源码有一行当作构建成功。
    payload = output / "payload"
    payload.mkdir()
    run("rpm", "--root", payload, "--dbpath", "/var/lib/rpm", "--initdb")
    run("rpm", "--root", payload, "--dbpath", "/var/lib/rpm", "--nodeps", "--noscripts",
        "-i", *sorted(rpms.glob("*.rpm")))
    drivers = driver_proof(payload)
    packages = query("rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n")
    (output / "builder-packages.tsv").write_text("\n".join(sorted(packages.splitlines())) + "\n")
    result = {"status": "FEDORA_MESA_BUILT_NOT_DEVICE_VERIFIED", "releasever": "44",
              "arch": "aarch64", "chip_id": hex(CHIP_ID), "source_url": SOURCE_URL,
              "source_rpm_sha256": SOURCE_SHA256, "source_tarball_sha256": TARBALL_SHA256,
              "original_spec_sha256": SPEC_SHA256, "effective_spec_sha256": sha256(spec),
              "patch_commit": lock["commit"], "patch_sha256": PATCH_SHA256,
              "driver_files": drivers, "packages": selected,
              "rpms": {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size}
                       for p in sorted(rpms.glob("*.rpm"))},
              "builder_packages_sha256": sha256(output / "builder-packages.tsv"),
              "compiler": query("gcc", "--version").splitlines()[0], "device_tested": False}
    (output / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sunuefi", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.sunuefi.resolve(), args.output.resolve())
