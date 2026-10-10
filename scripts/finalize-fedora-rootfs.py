#!/usr/bin/env python3
"""完成新构建的 Fedora rootfs：匹配模块、服务、身份清理和最终 manifest。"""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from piano_artifacts import guest_resolve, inspect_kernel, safe_destination, sha256

ROOT = Path(__file__).resolve().parents[1]


def chroot(root, *command):
    return subprocess.check_output(["chroot", str(root), *command], text=True)


def prune_missing_manpage_links(root):
    """nodocs 会留下 alternatives 与 man 目录两端的手册链接。"""
    missing = []
    for relative in ("etc/alternatives", "usr/share/man"):
        folder = safe_destination(root, relative)
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_symlink():
                target = guest_resolve(root, "/" + path.relative_to(root).as_posix())
                if target.is_relative_to(root / "usr/share/man") and not target.exists():
                    missing.append(path)
    # 先解析完整链再删除，避免删除 alternatives 后隐藏 man 端的最终目标。
    removed = sorted(path.relative_to(root).as_posix() for path in missing)
    for path in missing:
        path.unlink()
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("rootfs", "kernel", "runtime", "output"):
        ap.add_argument("--" + name, required=True, type=Path)
    args = ap.parse_args()
    root, kernel, runtime, output = [getattr(args, name).resolve() for name in ("rootfs", "kernel", "runtime", "output")]
    if not root.is_relative_to(ROOT / "build") or not root.is_dir() or os.geteuid() != 0:
        raise ValueError("finalize only a fresh build rootfs as root")
    m, kernel_hash = inspect_kernel(kernel)
    runtime_meta = json.loads((runtime / "manifest.json").read_text())
    if runtime_meta["kernel_manifest_sha256"] != kernel_hash:
        raise ValueError("runtime and rootfs select different kernel builds")
    for row in m["modules"]:
        path = root / "usr" / row["path"]
        if sha256(path) != row["sha256"]:
            raise ValueError("installed core kernel module changed: " + row["path"])
    proof = json.loads((runtime / "kernel-abi.json").read_text())
    loop = root / f"usr/lib/modules/{m['kernel_release']}/updates/v4l2loopback.ko"
    if proof["kernel_manifest_sha256"] != kernel_hash or sha256(loop) != proof["module_sha256"]:
        raise ValueError("installed camera module ABI or content differs")
    chroot(root, "/usr/sbin/depmod", "-a", m["kernel_release"])
    # 正式 root 的服务及账号在目标树中配置，绝不启动构建机上的设备服务。
    if subprocess.run(["chroot", str(root), "id", "piano"], capture_output=True).returncode:
        chroot(root, "useradd", "--create-home", "--groups", "wheel", "--shell", "/bin/bash", "piano")
    chroot(root, "usermod", "--password", "!", "piano")
    gdm = root / "etc/gdm/custom.conf"
    gdm.parent.mkdir(parents=True, exist_ok=True)
    gdm.write_text("[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin=piano\n")
    services = ["NetworkManager.service", "sshd.service", "gdm.service", "bluetooth.service",
                "piano-display.service", "piano-touch.service", "piano-radio.service",
                "piano-adsp.service", "piano-audio.service", "piano-keyboard.service",
                "piano-cpufreq.service", "piano-video.service", "piano-camera.service",
                "piano-camerad.service", "adsprpcd-sensorspd.service"]
    chroot(root, "systemctl", "enable", *services)
    # FastRPC 的默认 root/audio PD 自动启动与 piano 的顺序服务冲突。
    for unit in ("adsprpcd.service", "adsprpcd_audiopd.service", "adsprpcd-audiopd.service"):
        p = root / "etc/systemd/system" / unit
        if not p.exists() and not p.is_symlink():
            p.symlink_to("/dev/null")
    for name in ("dev", "proc", "sys", "run"):
        d = root / name
        if d.is_symlink():
            raise ValueError("runtime directory is an unexpected symlink: " + name)
        d.mkdir(exist_ok=True)
        for p in d.iterdir():
            if os.path.ismount(p):
                raise ValueError("builder mount remains in the rootfs: " + str(p))
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
    for key in (root / "etc/ssh").glob("ssh_host_*"):
        key.unlink()
    machine = root / "etc/machine-id"
    if machine.is_symlink():
        machine.unlink()
    machine.write_text("")
    dbus = root / "var/lib/dbus/machine-id"
    if dbus.exists() or dbus.is_symlink():
        dbus.unlink()
    dbus.parent.mkdir(parents=True, exist_ok=True)
    dbus.symlink_to("/etc/machine-id")
    removed_manpages = prune_missing_manpage_links(root)
    packages = chroot(root, "rpm", "-qa", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n")
    output.mkdir(parents=True, exist_ok=True)
    (output / "packages.tsv").write_text("".join(sorted(packages.splitlines(keepends=True))))
    required = {"piano-runtime", "libssc", "iio-sensor-proxy", "systemd", "gdm", "gnome-shell"}
    installed = {line.split("\t")[0] for line in packages.splitlines()}
    if not required <= installed:
        raise ValueError("missing required installed packages: " + str(required - installed))
    if any(name.startswith("kernel-core") for name in installed):
        raise ValueError("stock Fedora kernel entered the device rootfs")
    result = {"status": "FEDORA_DEVICE_ROOT_STAGED_NOT_BOOT_VERIFIED",
              "kernel_release": m["kernel_release"], "kernel_commit": m["source_commit"],
              "kernel_manifest_sha256": kernel_hash, "root_policy": "LABEL=PIANOROOT",
              "root_fstype": "f2fs", "runtime_manifest_sha256": sha256(runtime / "manifest.json"),
              "packages_sha256": sha256(output / "packages.tsv"), "enabled_services": services,
              "login": "piano autologin; password locked; owner provisions credentials",
              "device_configuration_required": ["/etc/piano/boot-request.json", "active Android slot for sensors"],
              "removed_manpage_links": removed_manpages, "device_tested": False}
    (output / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
