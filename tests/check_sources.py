#!/usr/bin/env python3
"""校验 sources.lock.json：结构、commit 格式、URL 唯一。"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "sources.lock.json"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def main():
    if not LOCK.is_file():
        return fail(f"missing {LOCK}")
    try:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return fail(f"invalid JSON: {exc}")

    sources = data.get("sources")
    if not isinstance(sources, dict) or not sources:
        return fail("sources must be a non-empty object")

    urls = {}
    for name, entry in sources.items():
        if not isinstance(entry, dict):
            return fail(f"{name}: entry must be an object")
        for field in ("url", "ref", "commit", "role"):
            if not entry.get(field):
                return fail(f"{name}: missing {field}")
        if not HEX40.match(entry["commit"]):
            return fail(f"{name}: commit must be 40 hex chars, got {entry['commit']!r}")
        if entry["url"] in urls:
            return fail(f"{name}: url duplicates {urls[entry['url']]}")
        urls[entry["url"]] = name

    print(f"OK: {len(sources)} sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
