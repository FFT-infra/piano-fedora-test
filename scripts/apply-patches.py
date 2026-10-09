#!/usr/bin/env python3
"""把本仓库的补丁应用到 SunUEFI checkout。

上游按 commit 锁定，改动不 fork 而是走补丁。每个补丁在 `series.json` 里
登记它要求的目标文件摘要：摘要不匹配就报错退出，不静默跳过，也不强行
应用一个上下文已经变化的补丁。

用法：
    scripts/apply-patches.py --sunuefi DIR
    scripts/apply-patches.py --sunuefi DIR --check
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHES = ROOT / "patches"


class PatchError(Exception):
    """输入或环境不满足，属于可预期失败。"""


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def load_series(folder):
    manifest = folder / "series.json"
    if not manifest.is_file():
        raise PatchError(f"missing series: {manifest}")
    try:
        series = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PatchError(f"invalid JSON in {manifest}: {exc}") from exc
    if series.get("schema_version") != 1:
        raise PatchError(f"unsupported series schema in {manifest}")
    return series


def verify_targets(sunuefi, series, patch):
    """应用前核对目标文件摘要。

    上游更新后目标文件会变，这里必须先失败，而不是让 git apply 用一个
    部分匹配的上下文改出错误结果。
    """
    for relative, expected in patch.get("targets", {}).items():
        path = sunuefi / relative
        if not path.is_file():
            raise PatchError(f"{patch['file']}: target missing: {relative}")
        actual = sha256(path)
        if actual != expected:
            raise PatchError(
                f"{patch['file']}: target changed: {relative}\n"
                f"  expected {expected}\n  actual   {actual}\n"
                "  上游已更新，需要重新生成补丁后再应用。"
            )


def check_created(sunuefi, patch):
    for relative in patch.get("creates", []):
        if (sunuefi / relative).exists():
            raise PatchError(f"{patch['file']}: refuses to overwrite {relative}")


def run_git_apply(sunuefi, patch_path, check_only):
    command = ["git", "apply", "--whitespace=error-all"]
    if check_only:
        command.append("--check")
    command.append(str(patch_path))
    result = subprocess.run(command, cwd=sunuefi, capture_output=True, text=True)
    if result.returncode:
        raise PatchError(
            f"{patch_path.name}: git apply failed\n{result.stderr.strip()}"
        )


def apply_folder(sunuefi, folder, check_only=False):
    series = load_series(folder)
    applied = []
    for patch in series["patches"]:
        patch_path = folder / patch["file"]
        if not patch_path.is_file():
            raise PatchError(f"missing patch file: {patch_path}")
        verify_targets(sunuefi, series, patch)
        check_created(sunuefi, patch)
        run_git_apply(sunuefi, patch_path, check_only)
        applied.append(patch["file"])
    return {"series": str(folder.relative_to(ROOT)), "patches": applied}


def discover():
    return sorted(
        path.parent
        for path in PATCHES.glob("*/series.json")
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sunuefi", required=True, help="SunUEFI checkout 路径")
    parser.add_argument("--check", action="store_true",
                        help="只验证能否应用，不写文件")
    parser.add_argument("--only", action="append", default=[],
                        help="只应用指定 series 目录名，可重复")
    args = parser.parse_args(argv)

    try:
        sunuefi = Path(args.sunuefi).resolve()
        if not (sunuefi / "build.sh").is_file():
            raise PatchError(f"not a SunUEFI checkout: {sunuefi}")

        folders = discover()
        if args.only:
            wanted = set(args.only)
            folders = [f for f in folders if f.name in wanted]
            missing = wanted - {f.name for f in folders}
            if missing:
                raise PatchError("unknown series: " + ", ".join(sorted(missing)))
        if not folders:
            raise PatchError(f"no patch series under {PATCHES}")

        results = [apply_folder(sunuefi, folder, args.check) for folder in folders]
        print(json.dumps(
            {"status": "CHECKED" if args.check else "APPLIED", "results": results},
            indent=2, ensure_ascii=False,
        ))
        return 0
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
