#!/usr/bin/env python3
"""给 Fedora rootfs 写入 piano 的 F2FS 根分区策略。

上游 `build_release_rootfs.py:apply_policy()` 里三处绑定 ext4：

1. fstab 写 `ext4 ... x-systemd.growfs`。systemd 的 resize_fs 只处理
   ext4/Btrfs/XFS，F2FS 会返回 EOPNOTSUPP，所以 growfs 必须去掉；容量按
   分区大小制作，不依赖首次开机扩容。
2. udev 放行规则要求 `ID_FS_TYPE=="ext4"`。不改的话 F2FS 根分区会被
   自己的保护规则设成只读。
3. 上游 mask 掉 `systemd-growfs-root.service` 的 RAM overlay 逻辑不适用。

这里做的是等价替换，不取消 Android 分区保护：只有 PIANOROOT/sunuefi_root
且类型是 f2fs 的那个分区被放行，其余匹配 UFS 的分区仍然设只读。

关机沿用 systemd 的同步与卸载，不移植未经本机验证的强制 shutdown hook。

用法：
    scripts/apply-rootfs-policy.py --rootfs DIR
"""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from piano_artifacts import safe_destination

ROOT = Path(__file__).resolve().parents[1]

ROOT_LABEL = "PIANOROOT"
ROOT_PARTNAME = "sunuefi_root"
ESP_LABEL = "SUNUEFI_ESP"
ESP_PARTNAME = "sunuefi_esp"

# 与上游一致的挂载策略，只把类型和选项换成 F2FS。
# data_flush/fsync_mode=strict 是 F2FS 根分区的常见取舍，与 sheng 相同。
FSTAB = (
    f"LABEL={ROOT_LABEL} / f2fs defaults,noatime,data_flush,fsync_mode=strict 0 0\n"
    f"LABEL={ESP_LABEL} /boot/efi vfat umask=0077,nofail 0 2\n"
)

UDEV_RULES = f'''SUBSYSTEM!="block", GOTO="release_end"
KERNELS!="1d84000.*", GOTO="release_end"
ENV{{UDISKS_IGNORE}}="1"
ENV{{DEVTYPE}}!="partition", GOTO="release_end"
IMPORT{{builtin}}="blkid"
ENV{{ID_FS_LABEL}}=="{ROOT_LABEL}", ENV{{ID_PART_ENTRY_NAME}}=="{ROOT_PARTNAME}", ENV{{ID_FS_TYPE}}=="f2fs", GOTO="release_end"
ENV{{ID_FS_LABEL}}=="{ESP_LABEL}", ENV{{ID_PART_ENTRY_NAME}}=="{ESP_PARTNAME}", ENV{{ID_FS_TYPE}}=="vfat", GOTO="release_end"
ATTR{{ro}}=="1", GOTO="release_end"
RUN+="/usr/sbin/blockdev --setro /dev/%k"
LABEL="release_end"
'''

# 关机沿用 systemd 的正常同步和卸载。

class PolicyError(Exception):
    """输入或环境不满足，属于可预期失败。"""


def write_file(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def apply(rootfs, root_label, root_partname, esp_label, esp_partname):
    rootfs = Path(rootfs).resolve()
    if not rootfs.is_dir():
        raise PolicyError(f"rootfs is not a directory: {rootfs}")
    # 这是 Fedora root，不是空的 staging 目录。
    if not (rootfs / "usr").is_dir():
        raise PolicyError(f"rootfs does not look like a system tree: {rootfs}")
    if (root_label, root_partname, esp_label, esp_partname) != (ROOT_LABEL, ROOT_PARTNAME, ESP_LABEL, ESP_PARTNAME):
        raise PolicyError("only the dedicated piano root/ESP identities are supported")
    for name in ("etc/fstab", "etc/piano/root-policy.json", "etc/udev/rules.d/01-piano-protect-android.rules"):
        safe_destination(rootfs, name)

    write_file(rootfs / "etc" / "fstab", FSTAB)
    write_file(
        rootfs / "etc" / "piano" / "root-policy.json",
        json.dumps({
            "version": 1,
            "mode": "label",
            "root_label": root_label,
            "root_partlabel": root_partname,
            "root_fstype": "f2fs",
            "esp_label": esp_label,
            "esp_partlabel": esp_partname,
        }, indent=2) + "\n",
    )
    write_file(
        rootfs / "etc" / "udev" / "rules.d" / "01-piano-protect-android.rules",
        UDEV_RULES,
    )
    # 不依赖首次开机扩容：容量按分区制作。去掉上游可能留下的 growfs 标记。
    growfs = rootfs / "etc" / "systemd" / "system" / "systemd-growfs-root.service"
    for unit in ("piano-swapfile.service", "qbootctl.service", "systemd-growfs-root.service"):
        p = rootfs / "etc/systemd/system" / unit
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink() and p.readlink() == Path("/dev/null"):
            continue
        if p.exists() or p.is_symlink():
            raise PolicyError(f"refuses to replace an existing service override: {unit}")
        p.symlink_to("/dev/null")

    return {
        "status": "ROOTFS_POLICY_APPLIED_NOT_BOOT_VERIFIED",
        "rootfs": str(rootfs),
        "root_fstype": "f2fs",
        "root_label": root_label,
        "root_partname": root_partname,
        "esp_label": esp_label,
        "esp_partname": esp_partname,
        "fstab": FSTAB.strip().splitlines(),
        "udev_rule": str((rootfs / "etc" / "udev" / "rules.d"
                          / "01-piano-protect-android.rules").relative_to(rootfs)),
        "shutdown": "systemd-sync-and-unmount",
        "masked_units": ["piano-swapfile.service", "qbootctl.service", "systemd-growfs-root.service"],
        "android_protection_kept": True,
        "device_tested": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rootfs", required=True)
    parser.add_argument("--root-label", default=ROOT_LABEL)
    parser.add_argument("--root-partname", default=ROOT_PARTNAME)
    parser.add_argument("--esp-label", default=ESP_LABEL)
    parser.add_argument("--esp-partname", default=ESP_PARTNAME)
    parser.add_argument("--manifest", help="把结果写入这个 JSON")
    args = parser.parse_args(argv)

    try:
        result = apply(args.rootfs, args.root_label, args.root_partname,
                       args.esp_label, args.esp_partname)
        if args.manifest:
            Path(args.manifest).write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (PolicyError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
