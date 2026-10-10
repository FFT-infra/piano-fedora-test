#!/usr/bin/env python3
"""离线扩展已验证 F2FS 根镜像，完整比较后输出可刷写的 Android sparse 镜像。"""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

from piano_artifacts import sha256, safe_destination
from piano_sparse import encode, inspect, require
from tree_metadata import compare

MIB = 1024**2


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def superblock(path):
    with path.open("rb") as stream:
        data = stream.read(8192)
    rows = []
    for start in (1024, 5120):
        require(len(data) >= start + 636, "truncated F2FS superblock")
        magic, log_block = struct.unpack_from("<I", data, start)[0], struct.unpack_from("<I", data, start + 16)[0]
        count = struct.unpack_from("<Q", data, start + 36)[0]
        label = data[start + 124:start + 636].decode("utf-16-le").split("\0", 1)[0]
        require(magic == 0xF2F52010 and log_block == 12 and label == "PIANOROOT", "invalid piano F2FS root superblock")
        rows.append((count, label))
    require(rows[0] == rows[1] and 0 < rows[0][0] * 4096 <= path.stat().st_size, "F2FS superblock capacity/copies differ")
    return {"filesystem_bytes": rows[0][0] * 4096, "label": rows[0][1]}


def expand_zstd(source, target, expected_bytes, expected_sha):
    with target.open("xb") as dest, subprocess.Popen(["zstd", "-dc", str(source)], stdout=subprocess.PIPE) as process:
        length = 0
        for data in iter(lambda: process.stdout.read(MIB), b""):
            length += len(data)
            require(length <= expected_bytes, "root expands beyond its sealed length")
            if any(data):
                dest.write(data)
            else:
                dest.seek(len(data), os.SEEK_CUR)
        require(process.wait() == 0 and length == expected_bytes, "root decompression failed or size differs")
        dest.truncate(length)
    require(sha256(target) == expected_sha, "uncompressed root digest differs")


def resize_and_verify(source, target, target_mib, work):
    require(os.geteuid() == 0, "F2FS mount verification requires root")
    require(type(target_mib) is int and 64 <= target_mib <= 1024**2, "target must be 64 MiB..1 TiB")
    require(target_mib * MIB >= source.stat().st_size, "shrinking an image is forbidden")
    require(not target.exists() and not target.is_symlink(), "output already exists")
    before = superblock(source)
    # GNU cp 保留洞；不修改通用交付镜像。
    run("cp", "--sparse=always", "--reflink=auto", "--no-clobber", source, target)
    with target.open("r+b") as stream:
        stream.truncate(target_mib * MIB)
    run("resize.f2fs", target)
    after = superblock(target)
    require(after["filesystem_bytes"] == target_mib * MIB, "F2FS did not expand to the target capacity")
    digest = sha256(target)
    fsck = run("fsck.f2fs", "-f", "--dry-run", target, capture_output=True, text=True)
    checks = [line for line in fsck.stdout.splitlines() if line.startswith("[FSCK]") and ("[Ok" in line or "[Fail" in line)]
    require(checks and not any("[Fail" in line for line in checks), "expanded filesystem consistency checks failed")
    require(sha256(target) == digest, "read-only fsck modified the expanded image")
    left, right = work / "source-mount", work / "target-mount"
    left.mkdir()
    right.mkdir()
    run("mount", "-t", "f2fs", "-o", "loop,ro,norecovery", source, left)
    try:
        run("mount", "-t", "f2fs", "-o", "loop,ro,norecovery", target, right)
        try:
            proof = compare(left, right)
        finally:
            run("umount", right)
    finally:
        run("umount", left)
    require(sha256(target) == digest, "readback modified the expanded root image")
    return {"source": before, "target": after, "expanded_sha256": digest,
            "expanded_bytes": target_mib * MIB, "metadata_verification": proof,
            "fsck_exit": fsck.returncode, "fsck_read_only": True,
            "fsck_consistency_checks": len(checks), "device_tested": False}


def configure_source(source, bundle, slot, key_file, work):
    require(slot in ("_a", "_b"), "current Android slot is required")
    fields = key_file.read_text().strip().split()
    require(len(fields) in (2, 3) and fields[0] == "ssh-ed25519", "provide one Ed25519 public key, never a private key")
    require(len(key_file.read_bytes()) <= 1024 and len(key_file.read_text().splitlines()) == 1, "invalid public-key file")
    blob = base64.b64decode(fields[1], validate=True)
    require(len(blob) == 51 and blob[:19] == struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32), "invalid Ed25519 public key")
    image = (bundle / "PianoUEFI-product.img").read_bytes()
    kernel, ramdisk = struct.unpack_from("<II", image, 8)
    offset = 4096 + (kernel + 4095) // 4096 * 4096
    app = image[offset:offset + ramdisk]
    require(len(app) == ramdisk and len(app) >= 64 and app[:16] == b"SUNUEFI-APPv1\0".ljust(16, b"\0")
            and hashlib.sha256(app[64:]).digest() == app[32:64], "invalid sealed APPv1")
    boot_request = {"version": 1, "boot_device": "/dev/disk/by-partlabel/boot" + slot,
                    "app_generation": hashlib.sha256(app).hexdigest()[:32]}
    mount = work / "configure-mount"
    mount.mkdir()
    run("mount", "-t", "f2fs", "-o", "loop", source, mount)
    try:
        paths = []
        config = safe_destination(mount, "etc/piano/boot-request.json")
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(boot_request, indent=2) + "\n")
        config.chmod(0o644)
        paths.append("/etc/piano")
        sensors_name = "etc/systemd/system/piano-sensors-import.service.d/android-slot.conf"
        sensors = safe_destination(mount, sensors_name)
        sensors.parent.mkdir(parents=True, exist_ok=True)
        sensors.write_text("[Service]\nEnvironment=PIANO_SENSORS_ODM_NAME=odm" + slot +
                           "\nEnvironment=PIANO_SENSORS_VENDOR_NAME=vendor" + slot + "\n")
        sensors.chmod(0o644)
        paths.append("/etc/systemd/system/piano-sensors-import.service.d")
        users = {}
        for line in (mount / "etc/passwd").read_text().splitlines():
            parts = line.split(":")
            if len(parts) == 7 and parts[0] in ("root", "piano"):
                users[parts[0]] = (int(parts[2]), int(parts[3]), parts[5])
        require(set(users) == {"root", "piano"}, "expected the prepared root and piano accounts")
        for name, (uid, gid, home) in users.items():
            require(home == ("/root" if name == "root" else "/home/piano"), "unexpected account home")
            directory = safe_destination(mount, home.lstrip("/") + "/.ssh")
            target = safe_destination(mount, home.lstrip("/") + "/.ssh/authorized_keys")
            directory.mkdir(mode=0o700, exist_ok=True)
            require(not target.exists(), "refuse to replace existing authorized keys")
            target.write_text(fields[0] + " " + fields[1] + "\n")
            directory.chmod(0o700)
            target.chmod(0o600)
            os.chown(directory, uid, gid)
            os.chown(target, uid, gid)
            paths.append(home + "/.ssh")
        # 使用镜像内匹配的 Fedora 工具及规则，不猜测 SSH/home 的 SELinux 类型。
        run("chroot", mount, "/usr/sbin/setfiles", "-F",
            "/etc/selinux/targeted/contexts/files/file_contexts", *paths)
        for name in ("etc/piano/boot-request.json", sensors_name, "root/.ssh/authorized_keys", "home/piano/.ssh/authorized_keys"):
            require(os.getxattr(mount / name, "security.selinux"), "device configuration lacks SELinux label")
    finally:
        run("umount", mount)
        mount.rmdir()
    return {"boot_request": boot_request, "android_slot": slot,
            "sensor_logical_partitions": ["odm" + slot, "vendor" + slot],
            "ssh_public_key_fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("="),
            "ssh_accounts": ["root", "piano"], "private_key_in_image": False}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--size-mib", type=int, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--android-slot", choices=("_a", "_b"))
    ap.add_argument("--ssh-public-key", type=Path)
    args = ap.parse_args()
    require(os.geteuid() == 0, "run with sudo")
    require(64 <= args.size_mib <= 1024**2, "target must be 64 MiB..1 TiB")
    require(bool(args.android_slot) == bool(args.ssh_public_key), "device provisioning requires both slot and public key")
    for name in ("zstd", "resize.f2fs", "fsck.f2fs", "cp", "mount", "umount"):
        require(shutil.which(name), "missing tool: " + name)
    spec = importlib.util.spec_from_file_location("bundle_verifier", Path(__file__).with_name("verify-bundle.py"))
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    bundle = args.bundle.resolve()
    verifier.verify(bundle)
    root_meta = json.loads((bundle / "root-image-manifest.json").read_text())
    output = args.output.absolute()
    require(not output.exists() and not output.is_symlink(), "use a new output directory")
    output.mkdir(parents=True)
    marker = output / ".incomplete"
    marker.write_text("Root installation image is not complete.\n")
    work = output / "work"
    work.mkdir()
    source, target = work / "source.raw", work / "expanded.raw"
    expand_zstd(bundle / "pianoroot.f2fs.img.zst", source, root_meta["bytes"], root_meta["sha256"])
    device_config = configure_source(source, bundle, args.android_slot, args.ssh_public_key, work) if args.android_slot else None
    proof = resize_and_verify(source, target, args.size_mib, work)
    sparse = output / "pianoroot.f2fs.sparse.img"
    transport = encode(target, sparse)
    decoded = inspect(sparse)
    require(decoded["expanded_sha256"] == proof["expanded_sha256"] and
            decoded["expanded_bytes"] == proof["expanded_bytes"], "sparse transport differs from verified F2FS bytes")
    record = {"schema_version": 1, "status": "ROOT_CAPACITY_VERIFIED_NOT_DEVICE_VERIFIED",
              "source_root_sha256": root_meta["sha256"],
              "source_bundle_manifest_sha256": sha256(bundle / "bundle-manifest.json"),
              "source_root_manifest_sha256": sha256(bundle / "root-image-manifest.json"),
              "target_size_mib": args.size_mib, "root_label": "PIANOROOT",
              "device_configuration": device_config,
              **proof, "transport": {**transport, **decoded, "file": sparse.name,
                                     "sha256": sha256(sparse), "bytes": sparse.stat().st_size}}
    manifest = output / "root-install-manifest.json"
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(sha256(p) + "  " + p.name + "\n" for p in (sparse, manifest)))
    # 仅清理本次新建、已完成验证的两个 raw 工作副本。
    source.unlink()
    target.unlink()
    left, right = work / "source-mount", work / "target-mount"
    left.rmdir()
    right.rmdir()
    work.rmdir()
    marker.unlink()
    print(json.dumps({"status": record["status"], "size_mib": args.size_mib, "transport": record["transport"]}))


if __name__ == "__main__":
    main()
