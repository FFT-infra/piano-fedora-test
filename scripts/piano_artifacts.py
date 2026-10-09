"""构建产物共用的身份、摘要与 guest 路径检查。"""

import hashlib
import json
import re
from pathlib import Path, PurePosixPath


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_relative(name):
    p = PurePosixPath(name)
    if p.is_absolute() or not name or any(x in ("", ".", "..") for x in name.split("/")):
        raise ValueError(f"unsafe relative path: {name!r}")
    return p


def guest_resolve(root, name):
    """guest 的绝对链接从 guest root 重新解析，绝不读取宿主目标。"""
    root = Path(root).resolve()
    pending, parts, hops = name.lstrip("/").split("/"), [], 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError("guest link escapes root")
            parts.pop()
            continue
        candidate = root.joinpath(*parts, part)
        if candidate.is_symlink():
            hops += 1
            if hops > 40:
                raise ValueError("guest symlink cycle")
            target = str(candidate.readlink())
            if target.startswith("/"):
                parts = []
            pending = target.split("/") + pending
        else:
            parts.append(part)
    return root.joinpath(*parts)


def safe_destination(root, name):
    """写入用 /usr 等 canonical 路径，拒绝经过任意 guest 符号链接。"""
    safe_relative(str(name))
    root = Path(root).resolve()
    p = root / name
    for parent in (p, *p.parents):
        if parent == root:
            return p
        if parent.is_symlink():
            raise ValueError(f"destination traverses a symlink: {name}")
    raise ValueError(f"destination is outside root: {name}")


def inspect_kernel(kernel, require_f2fs=True):
    """验证已封存的 Image、配置、每个模块及全部 modules.* 索引。"""
    kernel = Path(kernel).resolve()
    m = json.loads((kernel / "manifest.json").read_text())
    if m.get("status") != "HOST_BUILT_FULL_CANDIDATE_NOT_HARDWARE_VERIFIED" or not m.get("source_clean"):
        raise ValueError("kernel is not a completed clean full build")
    release = m.get("kernel_release", "")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", release):
        raise ValueError("invalid kernel release")
    for name, digest in (("Image", m["image"]["sha256"]), ("config", m["config_sha256"])):
        if sha256(kernel / name) != digest:
            raise ValueError(f"sealed kernel {name} changed")
    config = dict(line.split("=", 1) for line in (kernel / "config").read_text().splitlines()
                  if line.startswith("CONFIG_") and "=" in line)
    if require_f2fs:
        for symbol in ("F2FS_FS", "F2FS_FS_XATTR", "F2FS_FS_POSIX_ACL", "F2FS_FS_SECURITY"):
            if config.get("CONFIG_" + symbol) != "y":
                raise ValueError(f"kernel needs built-in CONFIG_{symbol}=y")
    if m.get("root_policy") != "LABEL=PIANOROOT":
        raise ValueError("kernel does not select the dedicated piano root")
    rows = m.get("modules", [])
    if not rows:
        raise ValueError("empty sealed module set")
    expected = set()
    for row in rows:
        name = row["path"]
        safe_relative(name)
        if not name.startswith(f"lib/modules/{release}/") or not name.endswith(".ko"):
            raise ValueError(f"module path/release mismatch: {name}")
        if name in expected or not row["vermagic"].startswith(release + " "):
            raise ValueError(f"module ABI or duplicate mismatch: {name}")
        expected.add(name)
        p = kernel / "modules" / name
        if p.is_symlink() or sha256(p) != row["sha256"] or p.stat().st_size != row["bytes"]:
            raise ValueError(f"sealed module changed: {name}")
    actual = {p.relative_to(kernel / "modules").as_posix()
              for p in (kernel / "modules").rglob("*.ko")}
    if actual != expected:
        raise ValueError("sealed module set differs from manifest")
    indexes = m["module_summary"]["index_sha256"]
    for name, digest in indexes.items():
        safe_relative(name)
        if not name.startswith(f"lib/modules/{release}/") or sha256(kernel / "modules" / name) != digest:
            raise ValueError(f"sealed module index changed: {name}")
    module_dir = kernel / "modules/lib/modules" / release
    if set(indexes) != {p.relative_to(kernel / "modules").as_posix() for p in module_dir.glob("modules.*") if p.is_file()}:
        raise ValueError("module index set differs from manifest")
    return m, sha256(kernel / "manifest.json")
