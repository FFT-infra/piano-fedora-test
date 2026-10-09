"""主机测试专用的封存格式 fixture；这些占位字节不用于实际构建。"""

import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_kernel(root, release):
    root = Path(root)
    modules = root / "modules/lib/modules" / release
    module_paths = [
        "kernel/drivers/iommu/arm-smmu.ko",
        "kernel/drivers/pinctrl/pinctrl-sm8750.ko",
        "kernel/drivers/mailbox/qcom-cpucp-mbox.ko",
        "kernel/drivers/phy/qualcomm/phy-qcom-qmp-ufs.ko",
        "kernel/drivers/ufs/ufs-qcom.ko",
    ]
    for name in module_paths:
        p = modules / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"TEST-FIXTURE-NOT-A-KERNEL-MODULE:" + name.encode())
    (modules / "modules.dep").write_text("".join(name + ":\n" for name in module_paths))
    (modules / "modules.builtin").write_text("")
    (modules / "modules.softdep").write_text("")
    (root / "config").write_text("".join("CONFIG_" + name + "=y\n" for name in
        ("F2FS_FS", "F2FS_FS_XATTR", "F2FS_FS_POSIX_ACL", "F2FS_FS_SECURITY")))
    (root / "Image").write_bytes(b"TEST-FIXTURE-NOT-A-BOOTABLE-KERNEL")
    record = {
        "status": "HOST_BUILT_FULL_CANDIDATE_NOT_HARDWARE_VERIFIED",
        "source_clean": True,
        "kernel_release": release,
        "source_commit": "352508459733d3e6d349ea5581a8dd2fd8bb4180",
        "root_policy": "LABEL=PIANOROOT",
        "image": {"sha256": digest(root / "Image")},
        "config_sha256": digest(root / "config"),
        "modules": [{
            "path": "lib/modules/" + release + "/" + name,
            "sha256": digest(modules / name), "bytes": (modules / name).stat().st_size,
            "vermagic": release + " SMP mod_unload aarch64",
        } for name in module_paths],
        "module_summary": {"index_sha256": {
            "lib/modules/" + release + "/" + p.name: digest(p)
            for p in modules.glob("modules.*")
        }},
    }
    (root / "manifest.json").write_text(json.dumps(record))
    return root
