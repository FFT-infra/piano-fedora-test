#!/usr/bin/env python3
"""导出自包含安装入口与固定上游的只读 GPT/BOOT 消费代码，不带任何设备数据。"""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil

from piano_artifacts import sha256
from piano_sparse import require

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sunuefi", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    spec = importlib.util.spec_from_file_location("installer_export", ROOT / "scripts/install-f2fs.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.load_source(args.sunuefi)
    output = args.output.absolute()
    require(not output.exists() and not output.is_symlink(), "installer output already exists")
    (output / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "sources.lock.json", output / "sources.lock.json")
    for name in ("install-f2fs.py", "plan-first-partition.py", "prepare-device-boot.py", "piano_artifacts.py", "piano_sparse.py"):
        shutil.copy2(ROOT / "scripts" / name, output / "scripts" / name)
    tools = output / "sunuefi/tools"
    tools.mkdir(parents=True)
    for name in ("install_piano.py", "provision_piano_bluetooth.py", "provision_piano_ssh.py", "compose_piano_dtb.py"):
        shutil.copy2(args.sunuefi / "tools" / name, tools / name)
    pin = json.loads((ROOT / "sources.lock.json").read_text())["sources"]["sunuefi"]["commit"]
    (output / "sunuefi/source-manifest.json").write_text(json.dumps({"commit": pin,
        "files": {"tools/" + p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(tools.iterdir())}}, indent=2) + "\n")
    shutil.copy2(ROOT / "docs/installation.md", output / "INSTALL.md")
    files = [p for p in sorted(output.rglob("*")) if p.is_file()]
    (output / "SHA256SUMS").write_text("".join(sha256(p) + "  " + p.relative_to(output).as_posix() + "\n" for p in files))
    print(json.dumps({"status": "INSTALLER_EXPORTED_NOT_DEVICE_VERIFIED", "files": len(files), "sunuefi_commit": pin}))


if __name__ == "__main__":
    main()
