#!/usr/bin/env python3
"""用 Actions 原生工具包装本机 BOOT，验证无损恢复和持久改选；不接触设备。"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from piano_artifacts import sha256
from piano_sparse import require


def checked_file(path, record):
    require(path.is_file() and not path.is_symlink(), "missing or symlink file: " + str(path))
    require(path.stat().st_size == record["bytes"] and sha256(path) == record["sha256"], "sealed file differs: " + str(path))


def prepare(stock, tools, host_tool, bundle, slot, source_sha, output):
    require(slot in ("_a", "_b"), "explicit Android slot required")
    require(stock.is_file() and not stock.is_symlink() and stock.stat().st_size == 96 * 1024**2,
            "expected the full current 96 MiB BOOT")
    require(sha256(stock) == source_sha, "BOOT does not match the measured current-device digest")
    require(not output.exists() and not output.is_symlink(), "use a new output directory")
    selector_meta = json.loads((tools / "selector-module.json").read_text())
    product = tools / "product"
    product_meta = json.loads((product / "manifest.json").read_text())
    require(product_meta.get("target") == "product", "expected the shared UEFI product core")
    for name in ("PianoUEFI-product.img", "BootShim.bin", "PianoUEFI-product.fd"):
        checked_file(product / name, product_meta["files"][name])
    require(sha256(product / "PianoUEFI-product.img") == sha256(bundle / "PianoUEFI-product.img"),
            "boot tools and Linux delivery bundle use different UEFI cores")
    selector = tools / "selector.bin"
    checked_file(selector, {"bytes": selector_meta["selector_bytes"], "sha256": selector_meta["selector_sha256"]})
    host_meta = json.loads((host_tool / "manifest.json").read_text())
    native_meta = json.loads((tools / "native/manifest.json").read_text())
    require(host_meta["sources"] == native_meta["sources"] and host_meta.get("arch") == "host",
            "host and Android repackers were built from different sources")
    native = host_tool / "piano-boot-repack"
    checked_file(native, host_meta["executable"])
    checked_file(tools / "native/piano-boot-repack", native_meta["executable"])
    native.chmod(0o755)
    output.mkdir(parents=True)
    marker = output / ".incomplete"
    marker.write_text("Device BOOT package has not completed.\n")

    def command(*args):
        result = subprocess.check_output([str(native), *map(str, args)], text=True)
        return json.loads(result)

    source_probe = command("probe", "--input", stock)
    require(source_probe["wrapped"] is False, "source BOOT already contains a wrapper")
    image = (product / "PianoUEFI-product.img").read_bytes()
    kernel, ramdisk = struct.unpack_from("<II", image, 8)
    start = 4096 + (kernel + 4095) // 4096 * 4096
    app = image[start:start + ramdisk]
    require(len(app) == ramdisk and len(app) >= 64 and app[:16] == b"SUNUEFI-APPv1\0".ljust(16, b"\0"), "invalid APPv1 envelope")
    require(hashlib.sha256(app[64:]).digest() == app[32:64], "APPv1 digest differs")
    app_sha = hashlib.sha256(app).hexdigest()
    require(selector_meta["app_payload_sha256"] == app_sha and
            selector_meta["product_fd_sha256"] == sha256(product / "PianoUEFI-product.fd"),
            "selector belongs to another APP/FD")
    app_path = output / "app-payload.bin"
    app_path.write_bytes(app)
    wrapped = output / "boot_android.img"
    repack = command("repack", "--input", stock, "--output", wrapped,
                     "--selector", selector,
                     "--selector-memory-bytes", selector_meta["selector_memory_bytes"],
                     "--selector-metadata-offset", selector_meta["metadata_offset"],
                     "--shim", product / "BootShim.bin", "--fd", product / "PianoUEFI-product.fd", "--app", app_path)
    app_path.unlink()
    restored = output / "restore-check.img"
    restore = command("restore", "--input", wrapped, "--output", restored)
    require(sha256(restored) == source_sha, "wrapped BOOT does not restore the complete original BOOT")
    restored.unlink()
    requests = {}
    previous = wrapped
    for name in ("linux", "android"):
        target = output / ("boot_linux.img" if name == "linux" else "request-roundtrip.img")
        requests[name] = command("request", "--input", previous, "--output", target, "--target", name)
        preview = command("request", "--input", target, "--target", name, "--preview")
        require(preview["target"] == (2 if name == "linux" else 0), "persistent request target differs")
        previous = target
    restore_after_request = command("restore", "--input", previous, "--output", restored)
    require(sha256(restored) == source_sha, "request changes broke byte-exact BOOT restoration")
    restored.unlink()
    previous.unlink()
    config = {"version": 1, "boot_device": "/dev/disk/by-partlabel/boot" + slot,
              "app_generation": app_sha[:32]}
    (output / "boot-request.json").write_text(json.dumps(config, indent=2) + "\n")
    record = {"schema_version": 1, "status": "DEVICE_BOOT_FILE_VERIFIED_NOT_DEVICE_BOOTED",
              "active_slot": slot, "source_boot_sha256": source_sha,
              "uefi_product_sha256": sha256(product / "PianoUEFI-product.img"),
              "host_tool_sha256": sha256(native), "native_tool_sha256": sha256(tools / "native/piano-boot-repack"),
              "source_probe": source_probe, "repack": repack, "restore": restore,
              "requests": requests, "restore_after_request": restore_after_request,
              "byte_equal_restoration": True, "boot_request": config,
              "device_passthrough_verified": False, "standard_recovery_verified": False,
              "device_operation_performed": False,
              "files": {name: {"bytes": (output / name).stat().st_size, "sha256": sha256(output / name)}
                        for name in ("boot_android.img", "boot_linux.img", "boot-request.json")}}
    manifest = output / "device-boot-manifest.json"
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(sha256(p) + "  " + p.name + "\n"
                                                for p in sorted(output.iterdir()) if p.is_file() and p != marker))
    marker.unlink()
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stock-boot", type=Path, required=True)
    ap.add_argument("--boot-tools", type=Path, required=True)
    ap.add_argument("--host-tool", type=Path, required=True)
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--slot", choices=("_a", "_b"), required=True)
    ap.add_argument("--source-sha256", required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    result = prepare(args.stock_boot, args.boot_tools, args.host_tool, args.bundle,
                     args.slot, args.source_sha256, args.output)
    print(json.dumps({"status": result["status"], "boot_request": result["boot_request"]}))


if __name__ == "__main__":
    main()
