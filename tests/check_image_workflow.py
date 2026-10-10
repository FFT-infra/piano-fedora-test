#!/usr/bin/env python3
"""用没有 Python 的模拟 Fedora 容器执行镜像 workflow 的 RPM 安装入口。"""

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        assert "--setopt=localpkg_gpgcheck=0" in arguments, arguments
    print("OK: RPM installation runs without Python in the Fedora container")


if __name__ == "__main__":
    main()
