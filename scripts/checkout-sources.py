#!/usr/bin/env python3
"""获取锁定源码和实际需要的传递依赖，输出供 Actions 消费的目录。"""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def checkout(url, commit, destination, full_history=False):
    if destination.exists():
        # Git checkout 会为未初始化的 gitlink 留一个空目录。
        if destination.is_dir() and not any(destination.iterdir()):
            destination.rmdir()
        else:
            raise ValueError(f"source output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["git", "clone", "--filter=blob:none", "--no-checkout"]
    if not full_history:
        subprocess.run(["git", "init", "-q", str(destination)], check=True)
        subprocess.run(["git", "-C", str(destination), "remote", "add", "origin", url], check=True)
        subprocess.run(["git", "-C", str(destination), "fetch", "--depth=1", "--filter=blob:none",
                        "origin", commit], check=True)
    else:
        subprocess.run([*command, url, str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "checkout", "--detach", commit], check=True)
    if git(destination, "rev-parse", "HEAD") != commit or git(destination, "status", "--porcelain"):
        raise ValueError(f"source identity/cleanliness mismatch: {destination}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--mode", choices=("check", "boot", "device", "graphics", "installer"), default="check")
    ap.add_argument("--kernel", action="store_true")
    args = ap.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "build"):
        raise ValueError("source outputs must stay in the ignored build directory")
    lock = json.loads((ROOT / "sources.lock.json").read_text())["sources"]
    sun = output / "sunuefi"
    checkout(lock["sunuefi"]["url"], lock["sunuefi"]["commit"], sun)
    upstream = json.loads((sun / "sources.lock.json").read_text())
    names = [] if args.mode == "check" else ["debian-piano-current"]
    if args.mode == "graphics":
        names = ["piano-mesa-current"]
    if args.mode == "installer":
        names = ["Mu-Silicium", "Mu-Silicium/Mu_Basecore", "simple-init"]
    if args.mode == "device":
        names += ["piano-firmware-current", "piano-sensors-current", "audioreach-topology", "v4l2loopback"]
    for name in names:
        path = "upstream/" + name
        owner = sun if "/" not in name else sun / "upstream" / name.rsplit("/", 1)[0]
        link = path if owner == sun else name.rsplit("/", 1)[1]
        registered = git(owner, "ls-files", "--stage", "--", link).split()
        if len(registered) < 2 or registered[0] != "160000" or registered[1] != upstream[name]["commit"]:
            raise ValueError(f"submodule registration differs from the upstream lock: {name}")
        subprocess.run(["git", "-C", str(owner), "submodule", "update", "--init", "--depth=1", link], check=True)
        if git(sun / path, "rev-parse", "HEAD") != upstream[name]["commit"]:
            raise ValueError(f"submodule pin differs: {name}")
    if args.kernel:
        kernel = lock["linux-piano"]
        checkout(kernel["url"], kernel["commit"], sun / "upstream/linux-piano", True)
    if args.mode == "device":
        mipps = lock["piano-mipps-auth"]
        checkout(mipps["url"], mipps["commit"], output / "piano-mipps-auth")
    subprocess.run(["python3", str(ROOT / "scripts/apply-patches.py"), "--sunuefi", str(sun)], check=True)
    print(json.dumps({"sunuefi": str(sun), "dependencies": names, "pins": {
        name: upstream[name]["commit"] for name in names}}, indent=2))


if __name__ == "__main__":
    main()
