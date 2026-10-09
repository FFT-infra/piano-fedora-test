#!/usr/bin/env python3
"""Fedora rootfs 后端：Fedora 容器 + dnf --installroot。

上游 SunUEFI 的 assemble_rootfs.py 只有 debootstrap 与 arch-tarball，没有 RPM
路径。本工具以独立后端实现同样的 plan/execute 契约，验证后再考虑回补上游。
机制与 mumuxiao722/fedora-sheng 相同，实现为独立重写。

用法：
    scripts/build-fedora-rootfs.py --plan --releasever 44
    scripts/build-fedora-rootfs.py --execute --releasever 44
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISTROS = ROOT / "build" / "distros"
PROFILES = ROOT / "scripts" / "profiles"


class BackendError(Exception):
    """输入或环境不满足，属于可预期失败。"""


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BackendError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BackendError(f"invalid JSON in {path}: {exc}") from exc


def container_runtime():
    for name in ("podman", "docker"):
        found = shutil.which(name)
        if found:
            return found
    return None


def plan(profile, desktops, releasever, desktop, output):
    if desktop not in desktops:
        known = ", ".join(sorted(desktops))
        raise BackendError(f"unknown desktop {desktop!r}; known: {known}")
    if not releasever or not str(releasever).isdigit():
        raise BackendError(
            "releasever must be an explicit numeric Fedora version. "
            "Set target.releasever in product.json or pass --releasever. "
            "It is left null on purpose, not guessed."
        )

    desk = desktops[desktop]
    packages = list(profile["base_packages"]) + list(desk["dnf_packages"])
    out = Path(output or DISTROS / f"fedora-{releasever}-{desktop}").resolve()
    if not out.is_relative_to(DISTROS.resolve()):
        raise BackendError(f"output must be under {DISTROS}")
    if out.exists():
        raise BackendError(f"output already exists: {out}")

    missing = []
    if container_runtime() is None:
        missing.append("podman or docker")

    result = {
        "status": "PLAN",
        "distro": "fedora",
        "releasever": str(releasever),
        "desktop": desktop,
        "arch": profile["arch"],
        "image": f"{profile['container_image']}:{releasever}",
        "output": str(out),
        "packages": packages,
        "dnf_options": profile["dnf_options"],
        "display_manager": desk.get("display_manager"),
        "missing_inputs": missing,
        "device_tested": False,
    }
    if missing:
        result["status"] = "INPUTS_REQUIRED"
    return result


def execute(result, profile):
    out = Path(result["output"])
    root = out / "rootfs"
    out.mkdir(parents=True)
    root.mkdir()
    runtime = container_runtime()
    if runtime is None:
        raise BackendError("need podman or docker to run dnf --installroot")
    if platform.machine() not in ("aarch64", "arm64"):
        raise BackendError("execute requires the native aarch64 Actions builder")

    cmd = [
        runtime, "run", "--rm",
        "-v", f"{root}:/mnt/rootfs:Z",
        result["image"],
        "dnf", "--installroot=/mnt/rootfs",
        f"--releasever={result['releasever']}",
        *profile["dnf_options"],
        "install", "-y", *result["packages"],
    ]
    with (out / "build.log").open("w") as stream:
        subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT, check=True)

    packages = subprocess.check_output([
        runtime, "run", "--rm", "-v", f"{root}:/mnt/rootfs:Z", result["image"],
        "rpm", "--root=/mnt/rootfs", "-qa",
        "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n",
    ], text=True)
    (out / "packages.tsv").write_text("".join(sorted(packages.splitlines(keepends=True))))
    result["container_id"] = subprocess.check_output([
        runtime, "image", "inspect", "--format", "{{.Id}}", result["image"]
    ], text=True).strip()
    result["packages_installed"] = len(packages.splitlines())
    result["device_layers_staged"] = False

    result["status"] = "BUILT_NOT_BOOT_VERIFIED"
    result["rootfs"] = str(root)
    (out / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--releasever", help="Fedora 版本号；缺省读 product.json")
    parser.add_argument("--desktop", help="gnome / kde / server")
    parser.add_argument("--output", help="输出目录，必须在 build/distros/ 下")
    parser.add_argument("--product", help="替代的 product.json 路径，用于测试与多目标")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    try:
        product = load_json(args.product or ROOT / "product.json")["target"]
        profile = load_json(PROFILES / "fedora.json")
        desktops = load_json(PROFILES / "fedora-desktops.json")

        if product["distro"] != "fedora":
            raise BackendError(f"product.json targets {product['distro']!r}, not fedora")

        releasever = args.releasever or product.get("releasever")
        desktop = args.desktop or product.get("desktop") or "gnome"
        result = plan(profile, desktops, releasever, desktop, args.output)

        if args.plan:
            # plan 永远以 0 退出：缺输入是计划里的一条事实，不是命令错误。
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if result["missing_inputs"]:
            raise BackendError(f"missing inputs: {', '.join(result['missing_inputs'])}")
        result = execute(result, profile)
        print(json.dumps({k: result[k] for k in ("status", "output", "rootfs")}, indent=2))
        return 0
    except BackendError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: build command failed with exit {exc.returncode}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
