#!/usr/bin/env python3
"""在 Actions 上恢复同一内核 SDK，构建匹配的 v4l2loopback 与 UAPI。"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from piano_artifacts import inspect_kernel, sha256


def run(command, **kw):
    subprocess.run([str(x) for x in command], check=True, **kw)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("sunuefi", "kernel", "sdk", "output"):
        ap.add_argument("--" + name, required=True, type=Path)
    args = ap.parse_args()
    sun, kernel, sdk, output = [getattr(args, name).resolve() for name in ("sunuefi", "kernel", "sdk", "output")]
    record, kernel_hash = inspect_kernel(kernel)
    if sha256(sdk / "kernel-sdk-manifest.json") != kernel_hash:
        raise ValueError("SDK does not belong to the selected sealed kernel")
    output.mkdir(parents=True)
    run(["python3", sun / "tools/prepare_release_kernel.py", "--refresh"], cwd=sun)
    prepared = json.loads((sun / "build/release-7.2.9/source-manifest.json").read_text())
    if (prepared["actual_commit"], prepared["actual_tree"]) != (record["source_commit"], record["source_tree"]):
        raise ValueError("replayed kernel source differs from the artifact")
    archive = next(sdk.glob("kernel-sdk-*.tar.zst"))
    restored = output / "sdk"
    restored.mkdir()
    run(["tar", "-I", "zstd", "-xf", archive, "-C", restored])
    if sha256(restored / ".config") != record["config_sha256"]:
        raise ValueError("SDK configuration differs from the sealed kernel")
    # 编译外部模块仍使用 Ubuntu 的同一 LLVM 工具链，Fedora 只编用户态程序。
    build = sun / "build/kernels/fedora-runtime"
    build.mkdir(parents=True)
    shutil.copy2(restored / ".config", build / ".config")
    shutil.copy2(restored / "Module.symvers", build / "Module.symvers")
    command = ["make", "-C", prepared["worktree"], "O=" + str(build),
               "ARCH=arm64", "LLVM=1", "LLVM_IAS=1"]
    run([*command, "-j4", "modules_prepare"])
    if sha256(build / ".config") != record["config_sha256"]:
        raise ValueError("module preparation changed the kernel configuration")
    uapi = output / "uapi"
    run([*command, "headers_install", "INSTALL_HDR_PATH=" + str(uapi)])
    loop = output / "v4l2loopback"
    shutil.copytree(sun / "upstream/v4l2loopback", loop,
                    ignore=shutil.ignore_patterns(".git"))
    run([*command, "-j4", "M=" + str(loop), "modules"])
    module = loop / "v4l2loopback.ko"
    release = subprocess.check_output(["modinfo", "-F", "vermagic", str(module)], text=True)
    if release.split()[0] != record["kernel_release"]:
        raise ValueError("external module vermagic differs from the selected kernel")
    proof = {"kernel_manifest_sha256": kernel_hash,
             "kernel_release": record["kernel_release"], "kernel_commit": record["source_commit"],
             "module_sha256": sha256(module), "vermagic": release.strip(),
             "sdk_config_sha256": sha256(build / ".config"),
             "module_symvers_sha256": sha256(build / "Module.symvers"), "device_tested": False}
    (output / "manifest.json").write_text(json.dumps(proof, indent=2) + "\n")


if __name__ == "__main__":
    main()
