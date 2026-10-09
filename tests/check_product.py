#!/usr/bin/env python3
"""校验 product.json 的结构与必填字段。null 表示待定，不是错误。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "product.json"
REQUIRED = (
    "distro", "desktop", "arch", "rootfs", "boot",
    "root_label", "esp_label", "esp_size_mib",
)


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def main():
    if not PRODUCT.is_file():
        return fail(f"missing {PRODUCT}")
    try:
        data = json.loads(PRODUCT.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return fail(f"invalid JSON: {exc}")

    target = data.get("target", {})
    for key in REQUIRED:
        if target.get(key) is None:
            return fail(f"target.{key} must be set")
    if target["arch"] != "aarch64":
        return fail(f"target.arch must be aarch64, got {target['arch']!r}")

    pending = sorted(k for k, v in target.items() if v is None)
    if pending:
        print(f"NOTE: pending: {', '.join(pending)}")
    print(f"OK: distro={target['distro']} rootfs={target['rootfs']} boot={target['boot']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
