#!/usr/bin/env python3
"""校验 sparse 的真实字节语义；--mount 在 Actions 上验证实际 F2FS 扩容。"""

import argparse
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from piano_artifacts import sha256
from piano_sparse import encode, decode_chunks, inspect


def load(name):
    spec = importlib.util.spec_from_file_location("capacity_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sparse_test(root):
    raw, sparse = root / "raw", root / "sparse"
    with raw.open("xb") as stream:
        stream.write(b"start" + bytes(4091))
        stream.seek(5 * 1024**2)
        stream.write(b"end" + bytes(4093))
        stream.truncate(7 * 1024**2)
    source = raw.read_bytes()
    encode(raw, sparse)
    assert b"".join(decode_chunks(sparse)) == source
    assert inspect(sparse)["expanded_sha256"] == sha256(raw)
    assert sparse.stat().st_size < len(source) // 2
    payload = bytearray(sparse.read_bytes())
    bad = root / "bad"
    for change in ("truncated", "trailing", "dont-care", "geometry"):
        data = bytearray(payload)
        if change == "truncated":
            data = data[:-1]
        elif change == "trailing":
            data += b"unexpected"
        elif change == "dont-care":
            struct.pack_into("<H", data, 28, 0xCAC3)
        else:
            struct.pack_into("<I", data, 16, 512)
        bad.write_bytes(data)
        try:
            inspect(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("accepted bad sparse transport: " + change)


def mount_test(root, target_mib):
    assert os.geteuid() == 0, "--mount requires root; CI must execute the real path"
    image, capacity = load("build-f2fs-image"), load("prepare-root-capacity")
    fixture, work = root / "fixture", root / "work"
    fixture.mkdir()
    work.mkdir()
    content = fixture / "payload"
    content.write_bytes(b"Fedora capacity fixture\n" * 2048)
    content.chmod(0o4755)
    os.setxattr(content, "user.piano.capacity", b"keep this exact value")
    os.setxattr(content, "security.capability", struct.pack("<IIIII", 0x02000001, 1 << 13, 0, 0, 0))
    os.setxattr(content, "security.selinux", b"system_u:object_r:bin_t:s0\0")
    os.link(content, fixture / "hardlink")
    (fixture / "link").symlink_to("payload")
    original, expanded = root / "source.img", root / "expanded.img"
    image.make_image(fixture, original, 128, "PIANOROOT", [])
    image.import_via_mount(original, fixture, work)
    initial = sha256(original)
    proof = capacity.resize_and_verify(original, expanded, target_mib, work)
    assert sha256(original) == initial, "capacity preparation changed the sealed source image"
    assert proof["target"]["filesystem_bytes"] == target_mib * 1024**2
    metadata = proof["metadata_verification"]
    assert metadata["entries"] >= 4 and metadata["capability_files"] == 2
    assert metadata["selinux_labels"] >= 2 and metadata["xattr_checked"]
    sparse = root / "expanded.sparse"
    encode(expanded, sparse)
    assert inspect(sparse)["expanded_sha256"] == proof["expanded_sha256"]
    print("OK: real F2FS expansion, full metadata and sparse roundtrip:", target_mib, "MiB")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mount", action="store_true")
    ap.add_argument("--target-mib", type=int, default=2048)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="piano-capacity-") as directory:
        root = Path(directory)
        sparse_test(root)
        if args.mount:
            mount_test(root, args.target_mib)
    print("OK: sparse byte equality; truncated, trailing and DONT_CARE chunks rejected")


if __name__ == "__main__":
    main()
