#!/usr/bin/env python3
"""不依赖 loop 权限的镜像验证回归测试；真实挂载另外由 Actions 验证。"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", type=Path, default=ROOT / "scripts/build-f2fs-image.py")
    args = ap.parse_args()
    spec = importlib.util.spec_from_file_location("image_tool", args.tool)
    image = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(image)
    real_run = subprocess.run
    failures = []

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        source = base / "source"
        source.mkdir()
        f = source / "payload"
        f.write_bytes(b"ABCDEF")
        f.chmod(0o4755)
        os.link(f, source / "hardlnk")
        (source / "link").symlink_to("payload")
        os.setxattr(f, "user.fixture", b"value")
        raw = base / "image"
        raw.write_bytes(b"fixture image bytes")

        def exercise(kind):
            work = base / kind
            work.mkdir()
            mounted, events = [False], []

            def mount_command(command, **kwargs):
                command = [str(x) for x in command]
                if command[0] == "mount":
                    mounted[0] = True
                    events.append("mount")
                    dest = Path(command[-1])
                    shutil.copytree(source, dest, dirs_exist_ok=True, symlinks=True)
                    # copytree 不保留硬链接；单独恢复同一 inode 的关系。
                    (dest / "hardlnk").unlink()
                    os.link(dest / "payload", dest / "hardlnk")
                    if kind == "content":
                        (dest / "payload").write_bytes(b"UVWXYZ")
                        shutil.copystat(source / "payload", dest / "payload")
                    elif kind == "symlink":
                        (dest / "link").unlink()
                        (dest / "link").symlink_to("hardlnk")
                    elif kind == "hardlink":
                        data = (dest / "hardlnk").read_bytes()
                        (dest / "hardlnk").unlink()
                        (dest / "hardlnk").write_bytes(data)
                        shutil.copystat(source / "hardlnk", dest / "hardlnk")
                    shutil.copystat(source, dest)
                    return subprocess.CompletedProcess(command, 0)
                if command[0] == "umount":
                    mounted[0] = False
                    events.append("umount")
                    return subprocess.CompletedProcess(command, 0)
                raise AssertionError(command)

            def fsck_result(command, **kwargs):
                assert command[0] == "fsck.f2fs", command
                events.append("fsck")
                if mounted[0]:
                    failures.append("fsck ran while the image was mounted")
                return subprocess.CompletedProcess(command, 0, "[FSCK] inode count [Ok..]\n", "")

            def capture(command):
                if command[0] == "fsck.f2fs":
                    return fsck_result(command).stdout.encode()
                return real_run([str(x) for x in command], check=True, capture_output=True).stdout

            try:
                with patch.object(image, "run", mount_command), \
                     patch.object(image, "capture", capture), \
                     patch.object(image.subprocess, "run", fsck_result):
                    image.verify(raw, source, work)
            except (ValueError, image.ImageError):
                if kind == "equal":
                    raise
                return
            if kind != "equal":
                failures.append(f"{kind} corruption was accepted")
            if not events or events[0] != "fsck":
                failures.append("consistency check must precede the read-only mount")

        for kind in ("equal", "content", "symlink", "hardlink"):
            exercise(kind)

    if failures:
        for failure in failures:
            print("FAIL:", failure, file=sys.stderr)
        return 1
    print("OK: same-size content corruption, link changes and mounted fsck are rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
