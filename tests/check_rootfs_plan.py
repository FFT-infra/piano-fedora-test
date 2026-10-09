#!/usr/bin/env python3
"""离线检查 Fedora 后端的 plan 与拒绝路径。不执行构建。"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "build-fedora-rootfs.py"


def run(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, cwd=cwd,
    )


def main():
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    # releasever 留空时必须拒绝，并说明是刻意留空而非猜测。
    # 用临时 product.json 测这条路径，不动仓库里的配置。
    with tempfile.TemporaryDirectory() as tmp:
        product = json.loads((ROOT / "product.json").read_text(encoding="utf-8"))
        product["target"]["releasever"] = None
        alt = Path(tmp) / "product.json"
        alt.write_text(json.dumps(product), encoding="utf-8")
        result = run("--plan", "--product", str(alt))
        check(result.returncode == 2, "empty releasever should exit 2")
        check("not guessed" in result.stderr, "should explain releasever is not guessed")

    # 正常 plan。
    result = run("--plan")
    check(result.returncode == 0, f"plan should succeed, got {result.returncode}")
    if result.returncode == 0:
        data = json.loads(result.stdout)
        check(data["distro"] == "fedora", "distro should be fedora")
        check(data["releasever"] == "44", "releasever should be 44")
        check(data["arch"] == "aarch64", "arch should be aarch64")
        check(data["device_tested"] is False, "plan must not claim device testing")
        check(len(data["packages"]) > 10, "plan should list packages")

    # 非数字版本：拒绝。
    check(run("--plan", "--releasever", "rawhide").returncode == 2, "rawhide should be rejected")

    # 未知桌面：拒绝。
    check(run("--plan", "--desktop", "dde").returncode == 2, "unknown desktop should be rejected")

    # 输出越界：拒绝。
    check(run("--plan", "--output", "/tmp/x").returncode == 2, "outside output should be rejected")

    # server 桌面：接受，且无显示管理器。
    result = run("--plan", "--desktop", "server")
    check(result.returncode == 0, "server desktop should be accepted")
    if result.returncode == 0:
        check(json.loads(result.stdout)["display_manager"] is None, "server should have no display manager")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1
    print("OK: plan and reject paths behave as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
