#!/usr/bin/env python3
"""离线检查补丁 series 的结构与拒绝路径。

不做真实应用（那需要 SunUEFI checkout），只核对登记信息和工具的参数处理。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "apply-patches.py"
PATCHES = ROOT / "patches"


def run(*args):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=ROOT,
    )


def main():
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    series_files = sorted(PATCHES.glob("*/series.json"))
    check(bool(series_files), "at least one patch series should exist")

    for manifest in series_files:
        folder = manifest.parent
        try:
            series = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{manifest}: invalid JSON: {exc}")
            continue

        check(series.get("schema_version") == 1, f"{manifest}: schema_version must be 1")
        check(bool(series.get("patches")), f"{manifest}: patches must not be empty")

        for patch in series.get("patches", []):
            name = patch.get("file")
            if not name:
                failures.append(f"{manifest}: a patch entry has no file")
                continue
            # 补丁文件必须真实存在，且不能带路径跳转。
            check(".." not in Path(name).parts, f"{manifest}: unsafe patch name {name}")
            check((folder / name).is_file(), f"{manifest}: missing patch file {name}")
            # 每个补丁都要有目标摘要，否则上游一变就会静默套用。
            targets = patch.get("targets") or {}
            check(bool(targets), f"{manifest}: {name} must pin its target digests")
            for relative, digest in targets.items():
                check(".." not in Path(relative).parts,
                      f"{manifest}: unsafe target path {relative}")
                check(isinstance(digest, str) and len(digest) == 64,
                      f"{manifest}: {relative} needs a sha256 digest")

    # 不是 SunUEFI checkout：拒绝。
    with tempfile.TemporaryDirectory() as tmp:
        check(run("--sunuefi", tmp).returncode == 2,
              "a directory without build.sh should be rejected")

    # 路径不存在：拒绝。
    check(run("--sunuefi", "/nonexistent-sunuefi").returncode == 2,
          "a missing directory should be rejected")

    # 未知 series：拒绝。
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp)
        (fake / "build.sh").write_text("#!/bin/sh\n")
        check(run("--sunuefi", str(fake), "--only", "no-such-series").returncode == 2,
              "an unknown series name should be rejected")

    # 补丁不能碰上游已有的文件。
    #
    # 这条是本轮 CI 失败换来的：补丁里混进过一个 .gitignore，本地 fixture
    # 没有这个文件所以没暴露，真实 checkout 上 git apply 直接拒绝覆盖。
    # 现在对每个补丁的 --check 都要求它在真实目录结构上可应用。
    sunuefi = ROOT.parent / "mipad8p-piano" / "sources" / "Project-SunUEFI"
    if (sunuefi / "build.sh").is_file():
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "sunuefi"
            # 只复制补丁会碰到的路径，避免搬整个仓库。
            for manifest in series_files:
                series = json.loads(manifest.read_text(encoding="utf-8"))
                for patch in series.get("patches", []):
                    for relative in (patch.get("targets") or {}):
                        source = sunuefi / relative
                        destination = work / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if source.is_file():
                            destination.write_bytes(source.read_bytes())
            (work / "build.sh").write_text("#!/bin/sh\n")
            # 把上游已有的文件也放进来，才能发现"补丁想创建已存在文件"。
            for existing in (".gitignore",):
                source = sunuefi / existing
                if source.is_file():
                    (work / existing).write_bytes(source.read_bytes())
            subprocess.run(["git", "init", "-q"], cwd=work, check=True)
            subprocess.run(["git", "add", "-A"], cwd=work, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "base"],
                cwd=work, check=True,
            )
            result = run("--sunuefi", str(work), "--check")
            check(result.returncode == 0,
                  f"patches must apply to the real upstream layout: {result.stderr.strip()}")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print(f"OK: {len(series_files)} patch series registered, real-layout check and reject paths behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
