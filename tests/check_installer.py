#!/usr/bin/env python3
"""验证首次 GPT 改写仅改变 userdata 尾端及两个空槽，不连接任何设备。"""

import argparse
import importlib.util
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sunuefi", type=Path)
    args = ap.parse_args()
    spec = importlib.util.spec_from_file_location("installer_test", ROOT / "scripts/install-f2fs.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    count, sectors = 64, 249518080
    table = bytearray(count * 128)
    guid = uuid.UUID("d55fa102-1ed9-4154-b3a7-3e84cd916cb5")
    rows = []
    for index in range(34):
        name = "userdata" if index == 33 else ("super" if index == 32 else "fixed_" + str(index))
        first, last = ((3632128, sectors - 6) if index == 33 else
                       (256, 3632127) if index == 32 else (6 + index * 2, 7 + index * 2))
        entry = memoryview(table)[index * 128:(index + 1) * 128]
        kind = uuid.UUID("ebd0a0a2-b9e5-4433-87c0-68b6b72699c7")
        identity = uuid.uuid5(guid, name)
        entry[:16], entry[16:32] = kind.bytes_le, identity.bytes_le
        struct.pack_into("<QQQ", entry, 32, first, last, 4)
        text = name.encode("utf-16-le")
        entry[56:56 + len(text)] = text
        rows.append({"index": index, "name": name, "first": first, "last": last,
                     "bytes": (last - first + 1) * 4096, "guid": str(identity), "type": str(kind), "attributes": 4})
    table = bytes(table)

    def header(here, alternate, table_at):
        result = bytearray(4096)
        struct.pack_into("<8sIIIIQQQQ16sQIII", result, 0, b"EFI PART", 0x10000, 92, 0, 0,
                         here, alternate, 6, sectors - 6, guid.bytes_le, table_at, count, 128, zlib.crc32(table))
        struct.pack_into("<I", result, 16, zlib.crc32(result[:92]))
        return bytes(result)

    def new_header(original, crc):
        data = bytearray(original)
        struct.pack_into("<I", data, 88, crc)
        struct.pack_into("<I", data, 16, 0)
        struct.pack_into("<I", data, 16, zlib.crc32(data[:92]))
        return bytes(data)

    primary, backup = header(1, sectors - 1, 2), header(sectors - 1, 1, sectors - 3)
    gpt = SimpleNamespace(disk_bytes=sectors * 4096, primary=primary, backup=backup,
                          primary_entries=table, backup_entries=table, partitions=tuple(rows), backup_table_lba=sectors - 3,
                          geometry={"count": 64, "first": 6, "last": sectors - 6, "disk_guid": str(guid), "table_lba": 2})
    if args.sunuefi:
        upstream = installer.load_source(args.sunuefi)
        gpt = upstream.parse_gpt(primary, table, backup, table, sectors * 4096)
    else:
        upstream = SimpleNamespace(_new_header=new_header, parse_gpt=lambda *unused: gpt,
            LINUX_TYPE=uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le,
            ESP_TYPE=uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b").bytes_le)
    mbr = bytes(510) + b"\x55\xaa" + bytes(3584)
    plan = installer.partition_plan(gpt)
    _, changed, original = installer.new_gpt(upstream, gpt, plan, mbr)
    assert len(original) == len(changed) == 1536 + len(table)
    new_table = changed[1536:]
    assert new_table[:33 * 128] == table[:33 * 128], "fixed Android partitions changed"
    assert new_table[33 * 128:33 * 128 + 40] == table[33 * 128:33 * 128 + 40]
    assert new_table[33 * 128 + 48:34 * 128] == table[33 * 128 + 48:34 * 128]
    assert struct.unpack_from("<Q", new_table, 33 * 128 + 40)[0] == 126509311
    for index, expected in ((34, "sunuefi_root"), (35, "sunuefi_esp")):
        assert new_table[index * 128 + 56:(index + 1) * 128].decode("utf-16-le").rstrip("\0") == expected
    assert new_table[36 * 128:] == table[36 * 128:], "unselected GPT slots changed"
    assert struct.unpack_from("<I", changed, 512 + 88)[0] == zlib.crc32(new_table)
    modified = dict(plan)
    modified["capacity"] = {**plan["capacity"], "linux_root_mib": 1}
    try:
        installer.new_gpt(upstream, gpt, modified, mbr)
    except ValueError:
        pass
    else:
        raise AssertionError("modified plan was accepted")
    print("OK: first GPT byte preservation, CRC, half capacity, deterministic new entries and stale-plan rejection" +
          ("; actual pinned parser exercised" if args.sunuefi else "; byte oracle exercised"))


if __name__ == "__main__":
    main()
