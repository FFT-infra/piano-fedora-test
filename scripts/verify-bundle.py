#!/usr/bin/env python3
"""验证交付集合的文件、内核/运行时/引导身份与 F2FS 完整检查记录。"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from piano_artifacts import sha256

REQUIRED = {"esp.img", "boot.img", "PianoUEFI-product.img", "pianoroot.f2fs.img.zst",
            "root-image-manifest.json", "kernel-manifest.json", "initramfs-manifest.json",
            "esp-manifest.json", "runtime-manifest.json", "source-runs.json"}


def verify(root):
    missing = REQUIRED - {p.name for p in root.iterdir() if p.is_file()}
    if missing:
        raise ValueError("bundle is incomplete: " + str(sorted(missing)))
    read = lambda name: json.loads((root / name).read_text())
    kernel = read("kernel-manifest.json")
    kernel_hash = sha256(root / "kernel-manifest.json")
    for name in ("initramfs-manifest.json", "runtime-manifest.json"):
        m = read(name)
        if m.get("kernel_manifest_sha256") != kernel_hash or m.get("kernel_release") != kernel["kernel_release"]:
            raise ValueError("bundle mixes kernel builds: " + name)
    esp = read("esp-manifest.json")
    if esp["kernel_manifest_sha256"] != kernel_hash:
        raise ValueError("ESP and rootfs use different kernel manifests")
    expected_paths = {"/EFI/Piano/stable/" + p for p in ("Image", "board.dtb", "initramfs", "boot.img")}
    if set(esp["esp_files"]) != expected_paths:
        raise ValueError("ESP layout differs from the actual firmware loader")
    for name in ("esp.img", "boot.img", "PianoUEFI-product.img"):
        row = esp["files"][name]
        if sha256(root / name) != row["sha256"] or (root / name).stat().st_size != row["bytes"]:
            raise ValueError("upstream-packaged component changed: " + name)
    image = read("root-image-manifest.json")
    if image["status"] != "IMAGE_VERIFIED" or image.get("format") != "raw-f2fs" or image["label"] != "PIANOROOT":
        raise ValueError("root image is not a verified piano F2FS image")
    proof = image["verify"]
    for key in ("file_content_checked", "numeric_owner_checked", "mode_checked", "symlink_targets_checked",
                "hardlink_groups_checked", "mtime_checked", "xattr_checked", "fsck_read_only", "fsck_ran_unmounted"):
        if proof.get(key) is not True:
            raise ValueError("root image verification is missing: " + key)
    if proof["fsck_exit"] != 0 or proof["fsck_changed_image"] or proof["xattr_read_errors_ignored"]:
        raise ValueError("root image verification did not complete safely")
    if proof.get("selinux_labels", 0) < 1:
        raise ValueError("Fedora SELinux labels were not applied and verified")
    raw_hash, expanded = hashlib.sha256(), 0
    with subprocess.Popen(["zstd", "-dc", str(root / "pianoroot.f2fs.img.zst")], stdout=subprocess.PIPE) as process:
        for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
            raw_hash.update(block)
            expanded += len(block)
        if process.wait() != 0:
            raise ValueError("compressed root image failed to decode")
    if expanded != image["bytes"] or raw_hash.hexdigest() != image["sha256"]:
        raise ValueError("compressed root image differs from the verified raw image")
    with (root / "boot.img").open("rb") as f:
        if f.read(8) != b"ANDROID!":
            raise ValueError("Linux boot payload is not an Android boot container")
    return {"status": "HOST_VERIFIED_FEDORA_F2FS_BUNDLE_NOT_DEVICE_VERIFIED",
            "kernel_release": kernel["kernel_release"], "kernel_manifest_sha256": kernel_hash,
            "root_image": {"format": "raw-f2fs", "label": "PIANOROOT", "expanded_sha256": image["sha256"],
                           "bytes": image["bytes"], "verification": proof},
            "source_runs": read("source-runs.json"), "device_tested": False,
            "device_operation_performed": False,
            "deployment_requirements": ["recoverable device backup", "userdata/data-handling choice",
                                        "current stock BOOT and slot/generation provisioning"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", required=True, type=Path)
    ap.add_argument("--write-manifest", action="store_true")
    args = ap.parse_args()
    root = args.bundle.resolve()
    result = verify(root)
    if args.write_manifest:
        result["files"] = {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size}
                           for p in sorted(root.iterdir()) if p.is_file() and p.name not in ("bundle-manifest.json", "SHA256SUMS")}
        (root / "bundle-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
        (root / "SHA256SUMS").write_text("".join(sha256(p) + "  " + p.name + "\n"
                                              for p in sorted(root.iterdir()) if p.is_file() and p.name != "SHA256SUMS"))
    print(json.dumps({"status": result["status"], "kernel_release": result["kernel_release"], "device_tested": False}))


if __name__ == "__main__":
    main()
