#!/usr/bin/env python3
"""首次安装 Fedora/F2FS：默认只读计划，显式执行才允许清空 userdata。"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import struct
import subprocess
import sys
import time
import uuid
import zlib

from piano_artifacts import sha256
from piano_sparse import require

ROOT = Path(__file__).resolve().parents[1]
BLOCK = 4096


def load_source(source):
    pin = json.loads((ROOT / "sources.lock.json").read_text())["sources"]["sunuefi"]["commit"]
    names = ("install_piano.py", "provision_piano_bluetooth.py", "provision_piano_ssh.py", "compose_piano_dtb.py")
    if (source / ".git").exists():
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        require(actual == pin, "installer source does not match sources.lock.json")
        for name in names:
            path = "tools/" + name
            expected = subprocess.check_output(["git", "-C", str(source), "show", "HEAD:" + path])
            require((source / path).read_bytes() == expected, "locked installer source changed: " + name)
    else:
        proof = json.loads((source / "source-manifest.json").read_text())
        require(proof["commit"] == pin and set(proof["files"]) == {"tools/" + name for name in names},
                "exported source manifest differs from the pinned installer")
        for name, record in proof["files"].items():
            checked(source / name, record)
    sys.path.insert(0, str(source / "tools"))
    import install_piano
    return install_piano


def load_planner():
    spec = importlib.util.spec_from_file_location("first_partition_planner", ROOT / "scripts/plan-first-partition.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def partition_plan(gpt):
    disk = {"sectors": gpt.disk_bytes // BLOCK, "logical_sector": BLOCK, "physical_sector": BLOCK,
            "entry_count": gpt.geometry["count"], "first_usable": gpt.geometry["first"],
            "last_usable": gpt.geometry["last"], "guid": gpt.geometry["disk_guid"]}
    rows = [{"number": row["index"] + 1, "first": row["first"], "last": row["last"],
             "name": row["name"], "code": row["type"], "size_text": str(row["bytes"])} for row in gpt.partitions]
    return load_planner().build_plan(disk, rows, 512)


def new_gpt(upstream, gpt, plan, mbr):
    require(partition_plan(gpt) == plan, "partition plan differs from the actual complete GPT")
    require(len(mbr) == BLOCK and mbr[510:512] == b"\x55\xaa", "protective MBR missing")
    table = bytearray(gpt.primary_entries)
    changes = {row["name"]: row for row in plan["changes"]}
    userdata = next(row for row in gpt.partitions if row["name"] == "userdata")
    struct.pack_into("<Q", table, userdata["index"] * 128 + 40, changes["userdata"]["new_last"])
    existing_guids = {row["guid"] for row in gpt.partitions}
    for name, kind in (("sunuefi_root", upstream.LINUX_TYPE), ("sunuefi_esp", upstream.ESP_TYPE)):
        row = changes[name]
        offset = (row["partition"] - 1) * 128
        require(not any(table[offset:offset + 128]), "new GPT entry is not completely empty")
        identity = uuid.uuid5(uuid.UUID(gpt.geometry["disk_guid"]), name + ":" + str(row["first"]) + ":" + str(row["last"]))
        require(str(identity) not in existing_guids, "new partition GUID collides")
        existing_guids.add(str(identity))
        entry = bytearray(128)
        entry[:16], entry[16:32] = kind, identity.bytes_le
        struct.pack_into("<QQQ", entry, 32, row["first"], row["last"], 0)
        text = name.encode("utf-16-le")
        entry[56:56 + len(text)] = text
        table[offset:offset + 128] = entry
    table = bytes(table)
    crc = zlib.crc32(table[:gpt.geometry["count"] * 128])
    primary, backup = upstream._new_header(gpt.primary, crc), upstream._new_header(gpt.backup, crc)
    after = upstream.parse_gpt(primary, table, backup, table, gpt.disk_bytes)
    # 每个固定 Android 分区的完整 128-byte entry 都必须字节不变。
    for row in gpt.partitions:
        if row["name"] != "userdata":
            offset = row["index"] * 128
            require(table[offset:offset + 128] == gpt.primary_entries[offset:offset + 128], "fixed partition changed")
    backup_file = mbr[:512] + primary[:512] + backup[:512] + table[:gpt.geometry["count"] * 128]
    original_file = mbr[:512] + gpt.primary[:512] + gpt.backup[:512] + gpt.primary_entries[:gpt.geometry["count"] * 128]
    return after, backup_file, original_file


def checked(path, meta):
    require(path.is_file() and not path.is_symlink(), "missing or symlink input: " + str(path))
    require(path.stat().st_size == meta["bytes"] and sha256(path) == meta["sha256"], "input hash/size differs: " + str(path))


def installation_plan(upstream, device, bundle, root_install, boot, stock_boot):
    require(not any((p / ".incomplete").exists() for p in (bundle, root_install, boot)), "incomplete input package")
    snapshot = device.inspect()
    require(device.text("blockdev --getpbsz " + snapshot["identity"]["disk"]) == str(BLOCK), "unexpected physical sector size")
    layout = partition_plan(snapshot["gpt"])
    slot = device.text("getprop ro.boot.slot_suffix")
    require(slot in ("_a", "_b"), "unknown active Android slot")
    root = json.loads((root_install / "root-install-manifest.json").read_text())
    combined = json.loads((boot / "device-boot-manifest.json").read_text())
    generic = json.loads((bundle / "bundle-manifest.json").read_text())
    image = json.loads((bundle / "root-image-manifest.json").read_text())
    require(generic.get("status") == "HOST_VERIFIED_FEDORA_F2FS_BUNDLE_NOT_DEVICE_VERIFIED", "generic bundle not verified")
    for name in ("esp.img", "boot.img", "PianoUEFI-product.img"):
        checked(bundle / name, generic["files"][name])
    require(root.get("status") == "ROOT_CAPACITY_VERIFIED_NOT_DEVICE_VERIFIED"
            and root["source_bundle_manifest_sha256"] == sha256(bundle / "bundle-manifest.json")
            and root["source_root_manifest_sha256"] == sha256(bundle / "root-image-manifest.json")
            and root["source_root_sha256"] == image["sha256"], "capacity image belongs to another bundle")
    require(root["target_size_mib"] == layout["capacity"]["linux_root_mib"]
            and root["expanded_bytes"] == root["target_size_mib"] * 1024**2, "root image does not match the equal partition plan")
    require(root["fsck_exit"] == 0 and root["fsck_read_only"] is True
            and root["transport"]["dont_care_chunks"] == 0
            and root["transport"]["zero_policy"] == "explicit-zero-fill", "root capacity/transport verification incomplete")
    for field in ("file_content_checked", "numeric_owner_checked", "mode_checked", "symlink_targets_checked",
                  "hardlink_groups_checked", "mtime_checked", "xattr_checked"):
        require(root["metadata_verification"].get(field) is True, "root metadata proof missing: " + field)
    require(root["transport"]["file"] == "pianoroot.f2fs.sparse.img", "unexpected root transport path")
    checked(root_install / "pianoroot.f2fs.sparse.img", root["transport"])
    require(combined.get("status") == "DEVICE_BOOT_FILE_VERIFIED_NOT_DEVICE_BOOTED"
            and combined["byte_equal_restoration"] is True and combined["active_slot"] == slot,
            "combined BOOT does not match the current slot or lacks lossless restore proof")
    require(combined["uefi_product_sha256"] == sha256(bundle / "PianoUEFI-product.img"), "BOOT and ESP use different UEFI cores")
    for name in ("boot_android.img", "boot_linux.img", "boot-request.json"):
        checked(boot / name, combined["files"][name])
    require(root["device_configuration"]["boot_request"] == combined["boot_request"]
            and root["device_configuration"]["android_slot"] == slot
            and root["device_configuration"]["ssh_accounts"] == ["root", "piano"], "root device configuration differs from BOOT")
    current_boot = device.text("sha256sum /dev/block/by-name/boot" + slot).split()[0]
    require(current_boot == combined["source_boot_sha256"], "current Android BOOT changed since packaging")
    require(stock_boot.is_file() and not stock_boot.is_symlink() and stock_boot.stat().st_size == 96 * 1024**2
            and sha256(stock_boot) == current_boot, "current original BOOT recovery file is missing or differs")
    # 擦除后 Android 必须能通过自己的 fs_mgr 重建加密 userdata；不在宿主格式化密文分区。
    fstab = device.text("cat /vendor/etc/fstab.*")
    rows = [line for line in fstab.splitlines() if re.search(r"\s/data\s", line) and not line.lstrip().startswith("#")]
    require(rows and all("formattable" in line.split()[-1].split(",") for line in rows),
            "Android userdata auto-format contract is unverified; do not repartition")
    require(device.text("getprop sys.boot_completed") == "1", "normal Android boot must be complete")
    return {"schema_version": 1, "status": "FIRST_INSTALL_READ_ONLY_PLAN", "device": snapshot["identity"],
            "android_slot": slot, "android_build": device.text("getprop ro.build.version.incremental"),
            "gpt_baseline": snapshot["gpt"].baseline(), "partition_plan": layout,
            "source_boot_sha256": current_boot,
            "inputs": {"bundle_manifest_sha256": sha256(bundle / "bundle-manifest.json"),
                       "root_manifest_sha256": sha256(root_install / "root-install-manifest.json"),
                       "boot_manifest_sha256": sha256(boot / "device-boot-manifest.json")},
            "android_data_preserved": False, "device_writes": False}, snapshot


def sync_file(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    pending = path.with_suffix(".pending")
    with pending.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    sync_dir(path.parent)


def factory(device, identity, slot):
    serial = identity["factory_serial"]
    for key, expected in (("serialno", serial), ("product", "piano"), ("unlocked", "yes"),
                          ("is-userspace", "no"), ("current-slot", slot[1:])):
        require(device.fastboot_var(serial, key) == expected, "factory bootloader identity differs: " + key)


def execute(upstream, device, plan, bundle, root_install, boot, stock_boot, session):
    fresh, snapshot = installation_plan(upstream, device, bundle, root_install, boot, stock_boot)
    require(fresh == plan, "device, GPT, slot or input package changed since the reviewed plan")
    require(not session.exists() and not session.is_symlink(), "use a new installation session directory")
    session.mkdir(mode=0o700, parents=True)
    identity, slot = plan["device"], plan["android_slot"]
    original_mbr = device.read(identity["disk"], 0, 1)
    expected_gpt, new_backup, old_backup = new_gpt(upstream, snapshot["gpt"], plan["partition_plan"], original_mbr)
    (session / "original-gpt.bin").write_bytes(old_backup)
    sync_file(session / "original-gpt.bin")
    (session / "new-gpt.bin").write_bytes(new_backup)
    sync_file(session / "new-gpt.bin")
    shutil.copyfile(stock_boot, session / "original-boot.img")
    sync_file(session / "original-boot.img")
    sync_dir(session)
    save(session / "plan.json", plan)
    journal = {"status": "PREPARED_NOT_WRITTEN", "completed": [], "device_writes": False,
               "original_boot_sha256": plan["source_boot_sha256"], "android_data_preserved": False}

    def stage(name):
        journal["completed"].append(name)
        save(session / "result.json", journal)

    try:
        stage("inputs_and_current_gpt_validated")
        # 临时 UEFI 必须运行并返回原 Android，随后才允许首次分区写入。
        device.reboot_bootloader()
        factory(device, identity, slot)
        device.call(["fastboot", "-s", identity["factory_serial"], "boot", str(bundle / "PianoUEFI-product.img")])
        output, errors = device.call(["fastboot", "-s", "SunUEFI-piano", "getvar", "product"], timeout=45)
        require(re.search(rb"product:\s*piano-sunuefi(?:\s|$)", output + errors), "temporary UEFI did not enumerate correctly")
        device.call(["fastboot", "-s", "SunUEFI-piano", "reboot"])
        device.call(["adb", "-s", device.serial, "wait-for-device"], timeout=180)
        deadline = time.monotonic() + 180
        while device.text("getprop sys.boot_completed") != "1":
            require(time.monotonic() < deadline, "Android did not finish returning from temporary UEFI")
            time.sleep(2)
        returned, returned_snapshot = installation_plan(upstream, device, bundle, root_install, boot, stock_boot)
        require(returned == plan, "original Android/GPT/BOOT did not return unchanged")
        stage("temporary_uefi_and_android_return_verified")
        # 普通 Android 旁路也先临时启动验证，仍不改持久 BOOT。
        device.reboot_bootloader()
        factory(device, identity, slot)
        device.call(["fastboot", "-s", identity["factory_serial"], "boot", str(boot / "boot_android.img")])
        device.call(["adb", "-s", device.serial, "wait-for-device"], timeout=180)
        deadline = time.monotonic() + 180
        while device.text("getprop sys.boot_completed") != "1":
            require(time.monotonic() < deadline, "Android did not return from the temporary combined BOOT")
            time.sleep(2)
        returned, _ = installation_plan(upstream, device, bundle, root_install, boot, stock_boot)
        require(returned == plan, "Android passthrough changed original BOOT or GPT")
        stage("temporary_combined_boot_android_passthrough_verified")
        remote = "/data/local/tmp/piano-first-install-" + uuid.uuid4().hex
        device.call(["adb", "-s", device.serial, "push", str(session / "new-gpt.bin"), remote])
        require(device.text("sha256sum " + remote).split()[0] == sha256(session / "new-gpt.bin"), "staged GPT differs")
        latest = device.inspect()
        require(latest["identity"] == identity and latest["gpt"].baseline() == plan["gpt_baseline"], "GPT changed immediately before writing")
        require(device.text("sha256sum /dev/block/by-name/boot" + slot).split()[0] == plan["source_boot_sha256"],
                "original BOOT changed immediately before partitioning")
        # 现有 /data 会被清空；此刻不写 Linux 数据，也不尝试重载正在使用的内核分区视图。
        journal["device_writes"] = True
        journal["status"] = "GPT_WRITE_STARTED"
        save(session / "result.json", journal)
        device.shell("sgdisk --load-backup=" + shlex.quote(remote) + " " + identity["disk"] + " && sync")
        gpt = expected_gpt
        measured = upstream.parse_gpt(device.read(identity["disk"], 1, 1),
            device.read(identity["disk"], gpt.geometry["table_lba"], gpt.geometry["table_blocks"]),
            device.read(identity["disk"], gpt.disk_bytes // BLOCK - 1, 1),
            device.read(identity["disk"], gpt.backup_table_lba, gpt.geometry["table_blocks"]), gpt.disk_bytes)
        require(measured.partitions == gpt.partitions and measured.geometry == gpt.geometry, "written GPT readback differs")
        stage("gpt_written_and_both_copies_readback_validated")
        device.reboot_bootloader()
        factory(device, identity, slot)
        for row in gpt.partitions:
            if row["name"] in ("userdata", "sunuefi_root", "sunuefi_esp"):
                require(int(device.fastboot_var(identity["factory_serial"], "partition-size:" + row["name"]), 16) == row["bytes"],
                        "bootloader did not load the new partition capacity")
        generic = json.loads((bundle / "bundle-manifest.json").read_text())
        root = json.loads((root_install / "root-install-manifest.json").read_text())
        combined = json.loads((boot / "device-boot-manifest.json").read_text())
        expected_hashes = {
            "sunuefi_esp": generic["files"]["esp.img"]["sha256"],
            "sunuefi_root": root["transport"]["sha256"],
            "boot" + slot: combined["files"]["boot_android.img"]["sha256"],
        }
        for name, image in (("sunuefi_esp", bundle / "esp.img"), ("sunuefi_root", root_install / "pianoroot.f2fs.sparse.img"),
                            ("boot" + slot, boot / "boot_android.img")):
            before = sha256(image)
            require(before == expected_hashes[name], "input image hash differs from validated manifest: " + name)
            device.call(["fastboot", "-s", identity["factory_serial"], "flash", name, str(image)], timeout=3600)
            require(sha256(image) == before, "input image changed during transfer")
            stage("factory_fastboot_accepted_" + name)
        journal.update(status="IMAGES_TRANSFERRED_FIRST_BOOT_PENDING", linux_boot_verified=False,
                       android_boot_verified=False, device_readback_verified=False,
                       remaining=["first Linux boot and filesystem readback", "Android encrypted userdata initialization", "both-way switching"])
        save(session / "result.json", journal)
        return journal
    except BaseException as error:
        journal.update(status="INSTALL_INTERRUPTED", error=str(error),
                       recovery_gpt=str(session / "original-gpt.bin"),
                       recovery_boot=str(session / "original-boot.img"),
                       recovery_note="GPT restore cannot recover erased Android data; inspect completed stages before recovery or reboot")
        save(session / "result.json", journal)
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("operation", choices=("plan", "apply"))
    ap.add_argument("--sunuefi", type=Path, required=True)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--root-install", type=Path, required=True)
    ap.add_argument("--device-boot", type=Path, required=True)
    ap.add_argument("--stock-boot", type=Path, required=True)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--session", type=Path)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--accept-data-loss", action="store_true")
    args = ap.parse_args()
    upstream = load_source(args.sunuefi)
    device = upstream.Device(args.serial)
    if args.operation == "plan":
        result, _ = installation_plan(upstream, device, args.bundle, args.root_install, args.device_boot, args.stock_boot)
        require(not args.plan.exists(), "plan output already exists")
        save(args.plan, result)
    else:
        require(args.execute and args.accept_data_loss and args.session,
                "apply requires --execute --accept-data-loss and a new --session directory")
        result = execute(upstream, device, json.loads(args.plan.read_text()), args.bundle,
                         args.root_install, args.device_boot, args.stock_boot, args.session)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
