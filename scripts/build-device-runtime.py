#!/usr/bin/env python3
"""在 Actions 的原生 Fedora 构建器内编译 piano 程序并打包 RPM。"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from piano_artifacts import inspect_kernel, sha256


def run(command, **kw):
    subprocess.run([str(x) for x in command], check=True, **kw)


def put(source, destination, executable=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if destination.is_symlink() and os.readlink(destination) == os.readlink(source):
            return
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copyfile(source, destination)
        destination.chmod(0o755 if executable else 0o644)


def rpm_package(name, payload, output, requires=(), provides=(), version="0.1.0"):
    """自有 RPM 只拥有实际载荷文件，不声明拥有整个 /usr 或系统目录。"""
    top = output / (name + "-rpmbuild")
    for folder in ("BUILD", "BUILDROOT", "SPECS", "SOURCES", "RPMS", "SRPMS"):
        (top / folder).mkdir(parents=True)
    archive = top / "SOURCES" / f"{name}-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(payload, arcname=f"{name}-{version}")
    paths = [p.relative_to(payload).as_posix() for p in sorted(payload.rglob("*"))
             if p.is_file() or p.is_symlink()]
    spec = top / "SPECS" / (name + ".spec")
    spec.write_text(
        "%global __os_install_post %{nil}\n"
        "%global debug_package %{nil}\n%global _build_id_links none\n"
        f"Name: {name}\nVersion: {version}\nRelease: 1%{{?dist}}\n"
        "Summary: Piano native runtime built from locked sources\n"
        "License: MIT AND Apache-2.0 AND BSD-2-Clause-Patent AND GPL-2.0-only\n"
        f"Source0: {name}-{version}.tar.gz\n"
        + "".join("Requires: " + r + "\n" for r in requires)
        + "".join("Provides: " + r + "\n" for r in provides)
        + "\n%description\nNative Piano payload; device boot is independently verified.\n"
        "\n%prep\n%setup -q\n\n%build\n\n%install\n"
        "mkdir -p %{buildroot}\ncp -a . %{buildroot}/\n\n%files\n"
        + "".join('"/' + p.replace('%', '%%').replace('"', '\\"') + '"\n' for p in paths)
        + "\n%changelog\n", encoding="utf-8")
    run(["rpmbuild", "--define", "_topdir " + str(top), "-bb", spec])
    rpms = list((top / "RPMS").rglob("*.rpm"))
    if not rpms:
        raise ValueError("rpmbuild produced no packages")
    for p in rpms:
        shutil.copyfile(p, output / "rpms" / p.name)
    return rpms


def sensors_source(output, name, version, archive_name, digest):
    pool = "libs/libssc" if name == "libssc" else "i/iio-sensor-proxy"
    archive = output / archive_name
    url = "https://deb.debian.org/debian/pool/main/" + pool + "/" + archive_name
    with urllib.request.urlopen(url, timeout=60) as incoming, archive.open("xb") as target:
        shutil.copyfileobj(incoming, target)
    if sha256(archive) != digest:
        raise ValueError("pinned sensor source archive changed: " + name)
    destination = output / (name + "-source")
    destination.mkdir()
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not (roots[0] / "meson.build").is_file():
        raise ValueError("unexpected sensor source layout: " + name)
    return roots[0]


def build_sensors(sun, output):
    """取固定源码而非 Debian 二进制，在 Fedora 库 ABI 上构建。"""
    sensor = sun / "upstream/piano-sensors-current"
    rows = [
        ("libssc", "0.4.4", "libssc_0.4.4.orig.tar.gz",
         "716d6bd6b34d2d753060c6b54c9a87e34fae75b724c763bf9ef487efa3621587"),
        ("iio-sensor-proxy", "3.9", "iio-sensor-proxy_3.9.orig.tar.bz2",
         "19a4ae99b1b8af603fc789841025f9747bf445418d69ec7389e75adb6c8186bf"),
    ]
    source_records = {}
    for name, version, archive, digest in rows:
        source = sensors_source(output, name, version, archive, digest)
        patches = list((sensor / "patches" / name).glob("*.patch"))
        if name == "libssc":
            # SunUEFI 的属性类型与 raw vector 补丁属于同一已锁定版本。
            patches += sorted((sun / "patches/piano-sensors").glob("000[23]*.patch"))
        for patch in sorted(patches):
            run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", patch], cwd=source)
        build = output / (name + "-build")
        run(["meson", "setup", build, source, "--prefix=/usr", "--libdir=lib64",
             "--buildtype=release"])
        run(["meson", "compile", "-C", build, "-j", "4"])
        payload = output / (name + "-payload")
        run(["meson", "install", "-C", build], env={**os.environ, "DESTDIR": str(payload)})
        rpm_package(name, payload, output, version=version)
        # 后一包需要前一包的 native headers、pkg-config 与 GI 元数据。
        run(["dnf", "-y", "install", *[p for p in (output / "rpms").glob(name + "-*.rpm")],
             "--setopt=localpkg_gpgcheck=0"])
        source_records[name] = {"version": version, "archive_sha256": digest,
                                "patches": {p.name: sha256(p) for p in patches}}
    return source_records


def build(args):
    if platform.machine() not in ("aarch64", "arm64"):
        raise ValueError("native Fedora aarch64 builder required")
    sun, kernel, output = args.sunuefi.resolve(), args.kernel.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError("choose a fresh runtime output")
    record, kernel_hash = inspect_kernel(kernel)
    output.mkdir(parents=True)
    (output / "rpms").mkdir()
    sys.path.insert(0, str(sun / "tools"))
    import build_piano_runtime_helpers as runtime
    import stage_piano_ram_hardware as hardware
    import build_boot_request
    public = sun / "upstream/debian-piano-current"
    runtime.repository(public, runtime.PUBLIC_COMMIT)
    payload = output / "piano-payload"
    payload.mkdir()
    build = output / "helpers"
    build.mkdir()
    compiler = shutil.which("gcc")
    flags = [compiler, "-O2", "-Wall", "-Wextra", "-Werror"]
    touch, touch_proof = runtime.derive_touch_source(public, build)
    camera, camera_proof = runtime.derive_camerad_source(public, build)
    pen_files, pen_proof = runtime.build_pen_core(build, flags)
    helpers = dict(pen_files)
    for name, (relative, pin, destination) in (runtime.PUBLIC_SOURCES | runtime.BSP_SOURCES).items():
        source = (sun if name in runtime.BSP_SOURCES else public) / relative
        if pin and sha256(source) != pin:
            raise ValueError("locked helper changed: " + name)
        entry = build / (name + "-entry.c")
        entry.write_text(runtime.entry_source(name))
        obj, exe = build / (name + ".o"), build / name
        effective = touch if name == "piano-touch-view" else camera if name == "piano-camerad" else source
        if name == "piano-touch-view":
            item = runtime.build_touch_binary(build, flags, args.uapi, effective, pen_proof)
        else:
            run([*flags, "-isystem", args.uapi / "include", "-Dmain=PianoOriginalMain", "-c", effective, "-o", obj])
            run([*flags, "-static", entry, obj, "-lm", "-o", exe])
            item = runtime.verify_elf(exe)
        run([exe, "--help"], timeout=10)
        helpers[destination] = {**item, "file": name, "mode": 0o755}
    for destination, row in helpers.items():
        # Fedora 42+ 合并 /usr/sbin；新程序使用 canonical /usr/bin。
        destination = destination.replace("usr/sbin/", "usr/bin/", 1)
        put(build / row["file"], payload / destination, row["mode"] & 0o111)

    # 原始设备服务先装入 staging，再用上游已审阅的 adapter 替换启动脚本。
    for p in (public / "rootfs/overlay/usr/lib/systemd/system").glob("piano-*.service"):
        if p.name not in ("piano-swapfile.service", "piano-hostkeys.service", "piano-usb.service"):
            put(p, payload / "usr/lib/systemd/system" / p.name)
    adapters = sun / "build/fedora-adapters"
    hardware.build(adapters, public)
    for p in adapters.rglob("*"):
        if p.is_file() and p.name != "manifest.json":
            put(p, payload / p.relative_to(adapters), p.stat().st_mode & 0o111)
    for p in (sun / "linux/bsp/common").rglob("*"):
        if p.is_file() or p.is_symlink():
            put(p, payload / p.relative_to(sun / "linux/bsp/common"), p.stat().st_mode & 0o111)
    for name in ("cpufreq-start", "pstore-save"):
        put(public / "rootfs/overlay/usr/lib/piano" / name, payload / "usr/lib/piano" / name, True)
    put(Path(shutil.which("busybox")), payload / "usr/lib/piano/busybox", True)

    fw = sun / "upstream/piano-firmware-current"
    run(["sha256sum", "--check", "--quiet", "SHA256SUMS"], cwd=fw / "firmware")
    shutil.copytree(fw / "firmware", payload / "usr/lib/firmware", dirs_exist_ok=True)
    shutil.copytree(fw / "LICENSES", payload / "usr/share/doc/piano-firmware/LICENSES")
    run(["bash", public / "scripts/build-topology.sh", sun / "upstream/audioreach-topology", build / "firmware"])
    topology = build / "firmware/qcom/sm8750/Xiaomi Pad 8 Pro-tplg.bin"
    run(["alsatplg", "-d", topology, "-o", build / "topology-decoded.conf"])
    put(topology, payload / "usr/lib/firmware/qcom/sm8750/Xiaomi Pad 8 Pro-tplg.bin")
    if args.loopback:
        put(args.loopback, payload / f"usr/lib/modules/{record['kernel_release']}/updates/v4l2loopback.ko")
    request = build_boot_request.build(build / "boot-request", "aarch64", "gcc")
    put(build / "boot-request/piano-boot-request", payload / "usr/local/sbin/piano-boot-request", True)
    for name, path in (
        ("piano-next-boot", "usr/bin/piano-next-boot"),
        ("piano-next-boot-helper", "usr/libexec/piano-next-boot-helper"),
        ("piano-next-boot-configure", "usr/bin/piano-next-boot-configure"),
        ("org.sunuefi.boot-request.policy", "usr/share/polkit-1/actions/org.sunuefi.boot-request.policy"),
        ("49-piano-boot-request.rules", "usr/share/polkit-1/rules.d/49-piano-boot-request.rules"),
        ("org.sunuefi.ReturnAndroid.desktop", "usr/share/applications/org.sunuefi.ReturnAndroid.desktop"),
        ("org.sunuefi.BootMenu.desktop", "usr/share/applications/org.sunuefi.BootMenu.desktop"),
    ):
        put(sun / "linux/userspace" / name, payload / path, name.startswith("piano-"))
    mipps = sun.parent / "piano-mipps-auth"
    for name, path in (("xiaomi-mipps-auth", "usr/libexec/xiaomi-mipps-auth"),
                       ("xiaomi-mipps-auth.service", "usr/lib/systemd/system/xiaomi-mipps-auth.service"),
                       ("90-xiaomi-mipps-auth.rules", "usr/lib/udev/rules.d/90-xiaomi-mipps-auth.rules")):
        put(mipps / name, payload / path, name == "xiaomi-mipps-auth")
    sensor_proof = build_sensors(sun, output)
    sensor = sun / "upstream/piano-sensors-current"
    integration = output / "piano-sensors"
    shutil.copytree(sensor / "piano-sensors", integration)
    run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i",
         sun / "patches/piano-sensors/0001-import-vendor-reg-config.patch"], cwd=output)
    # 该补丁的根路径是 piano-sensors/，保持对应目录名。
    for name in ("piano-sensors-import", "piano-sensors-lp-table", "piano-sensors-wait"):
        put(integration / name, payload / "usr/libexec/piano-sensors" / name, True)
    for name in ("adsprpcd-sensorspd.service", "piano-sensors-import.service"):
        put(integration / "debian" / ("piano-sensors." + name), payload / "usr/lib/systemd/system" / name)
    put(integration / "debian/piano-sensors.udev", payload / "usr/lib/udev/rules.d/70-piano-sensors.rules")
    put(integration / "59-fastrpc-remoteproc.rules", payload / "etc/udev/rules.d/59-fastrpc-remoteproc.rules")
    put(integration / "iio-sensor-proxy-piano.conf", payload / "etc/systemd/system/iio-sensor-proxy.service.d/piano.conf")
    provenance = {"status": "FEDORA_RUNTIME_BUILT_NOT_DEVICE_VERIFIED",
                  "kernel_release": record["kernel_release"], "kernel_manifest_sha256": kernel_hash,
                  "touch": touch_proof, "camera": camera_proof, "boot_request": request,
                  "sensor_sources": sensor_proof, "device_tested": False}
    dest = payload / "usr/share/piano-provenance/fedora-runtime.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(provenance, indent=2) + "\n")
    rpm_package("piano-runtime", payload, output,
                requires=("python3", "busybox", "kmod", "util-linux", "fastrpc", "qrtr", "rmtfs", "tqftpserv", "alsa-utils"))
    provenance["rpms"] = {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size}
                          for p in (output / "rpms").glob("*.rpm")}
    (output / "manifest.json").write_text(json.dumps(provenance, indent=2) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("sunuefi", "kernel", "uapi", "output"):
        ap.add_argument("--" + name, type=Path, required=True)
    ap.add_argument("--loopback", type=Path, required=True)
    build(ap.parse_args())


if __name__ == "__main__":
    main()
