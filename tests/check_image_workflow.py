#!/usr/bin/env python3
"""用没有 Python 的模拟 Fedora 容器执行镜像 workflow 的 RPM 安装入口。"""

import hashlib
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def check_mesa(root):
    mesa = module("mesa_builder", ROOT / "scripts/build-fedora-mesa.py")
    header = bytearray(64)
    header[:5] = b"\x7fELF\x02"
    struct.pack_into("<H", header, 18, 183)
    data = bytes(header) + struct.pack("<Q", 0xffff44050001) + b"Adreno (TM) 830v1\0"
    library = root / "usr/lib64"
    library.mkdir(parents=True)
    for name in ("libgallium-26.2.3.so", "libvulkan_freedreno.so"):
        (library / name).write_bytes(data)
    assert len(mesa.driver_proof(root)) == 2
    (library / "libgallium-26.2.3.so").write_bytes(data.replace(
        struct.pack("<Q", 0xffff44050001), struct.pack("<Q", 0xffff44050000)))
    try:
        mesa.driver_proof(root)
    except ValueError as error:
        assert "lacks" in str(error)
    else:
        raise AssertionError("a generic A830 device table was accepted for piano")


def check_bundle(root, verifier=ROOT / "scripts/verify-bundle.py"):
    bundle = module("bundle_verifier", verifier)
    root.mkdir()
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    write = lambda name, value: (root / name).write_text(json.dumps(value))
    raw = b"verified fixture raw bytes"
    for name in ("esp.img", "PianoUEFI-product.img", "pianoroot.f2fs.img.zst"):
        (root / name).write_bytes(b"fixture")
    (root / "boot.img").write_bytes(b"ANDROID!fixture")
    write("kernel-manifest.json", {"kernel_release": "fixture"})
    kernel = digest(root / "kernel-manifest.json")
    identity = {"kernel_release": "fixture", "kernel_manifest_sha256": kernel}
    for name in ("runtime", "initramfs"):
        write(name + "-manifest.json", identity)
    write("mesa-manifest.json", {"status": "FEDORA_MESA_BUILT_NOT_DEVICE_VERIFIED", "chip_id": "0xffff44050001"})
    write("rootfs-manifest.json", {**identity, **{name + "_manifest_sha256": digest(root / (name + "-manifest.json"))
                                                for name in ("runtime", "mesa")}})
    write("esp-manifest.json", {"kernel_manifest_sha256": kernel,
          "esp_files": ["/EFI/Piano/stable/" + name for name in ("Image", "board.dtb", "initramfs", "boot.img")],
          "files": {name: {"sha256": digest(root / name), "bytes": (root / name).stat().st_size}
                    for name in ("esp.img", "boot.img", "PianoUEFI-product.img")}})
    proof = {key: True for key in ("file_content_checked", "numeric_owner_checked", "mode_checked", "symlink_targets_checked",
                                   "hardlink_groups_checked", "mtime_checked", "xattr_checked", "fsck_read_only", "fsck_ran_unmounted")}
    proof.update(fsck_exit=0, fsck_changed_image=False, xattr_read_errors_ignored=False, selinux_labels=1)
    image = {"status": "IMAGE_VERIFIED", "format": "raw-f2fs", "label": "PIANOROOT", "verify": proof,
             "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
             **{name + "_manifest_sha256": digest(root / (name + "-manifest.json"))
                for name in ("rootfs", "kernel", "runtime", "mesa", "initramfs")}}
    write("root-image-manifest.json", image)
    write("source-runs.json", {"kernel_run": "fixture"})

    def verify(data=raw):
        with patch.object(bundle.subprocess, "Popen") as spawn:
            process = spawn.return_value.__enter__.return_value
            process.stdout, process.wait.return_value = io.BytesIO(data), 0
            value = bundle.verify(root)
            spawn.assert_called_once()
            return value

    assert verify()["device_tested"] is False
    for name, data in (("mesa", raw), (None, b"wrong raw image payload")):
        write("root-image-manifest.json", {**image, **({name + "_manifest_sha256": "0" * 64} if name else {})})
        try:
            verify(data)
        except ValueError:
            pass
        else:
            raise AssertionError("bundle accepted mixed component metadata or corrupted raw bytes")


def install_step():
    lines = (ROOT / ".github/workflows/build-image.yml").read_text().splitlines()
    start = lines.index("      - name: Install the native Fedora device RPMs")
    run = lines.index("        run: |", start) + 1
    body = []
    for line in lines[run:]:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line[10:])
    assert body, "RPM installation step is empty"
    return "\n".join(body)


def executable(path, text):
    path.write_text("#!/bin/bash\nset -euo pipefail\n" + text)
    path.chmod(0o755)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        check_mesa(root / "mesa")
        check_bundle(root / "bundle")
        host, container = root / "host", root / "container"
        host.mkdir()
        container.mkdir()
        # 宿主可以解析 product.json；容器只提供 bash 与 dnf。
        (container / "bash").symlink_to("/bin/bash")
        executable(host / "sudo", 'exec "$@"\n')
        executable(host / "podman", '''
while [ "$#" -gt 0 ]; do
    case "$1" in
        run|--rm) shift ;;
        -v|-w) shift 2 ;;
        --env|-e) export "$2"; shift 2 ;;
        registry.fedoraproject.org/fedora:44) shift; break ;;
        *) printf 'Unexpected podman argument: %s\\n' "$1" >&2; exit 2 ;;
    esac
done
PATH="$PIANO_CONTAINER_PATH" exec "$@"
''')
        executable(container / "dnf", 'printf "%s\\n" "$@" >"$PIANO_CAPTURE"\n')
        capture = root / "dnf-arguments"
        env = {**os.environ, "PATH": str(host) + os.pathsep + os.environ["PATH"],
               "PIANO_CONTAINER_PATH": str(container), "PIANO_CAPTURE": str(capture),
               "GITHUB_WORKSPACE": str(ROOT)}
        result = subprocess.run(["/bin/bash", "-euo", "pipefail", "-c", install_step()],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        arguments = capture.read_text().splitlines()
        assert "--releasever=44" in arguments, arguments
        assert "--installroot=/workspace/build/device-root/rootfs" in arguments, arguments
        assert "install" in arguments and "/workspace/build/runtime/rpms/*.rpm" in arguments, arguments
        assert "/workspace/build/mesa/rpms/*.rpm" in arguments, arguments
        assert "--setopt=localpkg_gpgcheck=0" in arguments, arguments
    print("OK: container entry, A830v1 device tables and sealed bundle identity checks")


if __name__ == "__main__":
    main()
