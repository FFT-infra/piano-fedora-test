# 部署产物

部署一台 piano 需要六样东西。SunUEFI 提供启动链，本仓库提供 Fedora 侧的
rootfs、F2FS 策略与镜像。

| 产物 | 谁产出 | 状态 |
| --- | --- | --- |
| UEFI 固件（合体 BOOT） | `build-uefi.yml` | 已在 CI 构建 |
| 内核 Image + modules | `build-kernel.yml` | 已在 CI 构建；F2FS 补丁后正在重编 |
| Fedora rootfs 树 | `build-rootfs.yml` | 已在 CI 构建 |
| initramfs（F2FS） | `build-initramfs.yml` | 已实现，本机离线验证通过 |
| F2FS root 镜像 | `build-image.yml` | 已实现；新增策略与载荷步骤 |
| ESP 与 boot.img | `build-bundle.yml` | 已改为调用上游打包器 |

## 构建顺序

产物之间有依赖，触发顺序固定：

    build-kernel.yml ─┐
    build-uefi.yml   ─┼─→ build-bundle.yml
    build-rootfs.yml ─┴─→ build-initramfs.yml ─→ build-image.yml ─┘

`build-image.yml` 与 `build-bundle.yml` 各自按 artifact 名回溯到产出它的
那次 run，不依赖触发顺序。

## 内核

`build-kernel.yml` 按 `sources.lock.json` 取 SunUEFI 与内核的固定 commit，
先应用本仓库补丁（`scripts/apply-patches.py`），再跑
`prepare_release_kernel` 与 `build_piano_full_kernel`。约 2.5 小时。

补丁让内核内建 F2FS，并让硬件准备接受 F2FS 根分区。没有它，CI 产出的内核
里 `CONFIG_F2FS_FS` 是关的，root 镜像做得再对也挂不上。

## rootfs 侧的三步处理

`build-image.yml` 在制镜像前对 rootfs 做三步，缺一步启动就会失败：

1. `scripts/stage-rootfs-payload.py` 把内核模块装进
   `usr/lib/modules/<release>`，并放 `piano-ram-hardware-prepare` 与它依赖的
   两个 DMA 校验器。引导脚本要求这两条路径都存在，缺一条进救援 shell。
2. `scripts/apply-rootfs-policy.py` 写 F2FS 版 fstab、udev 放行规则和关机
   hook。上游版本写的是 ext4，F2FS 根会被自己的保护规则设成只读。
3. `scripts/build-f2fs-image.py` 挂载真实 F2FS，用 `tar --xattrs --acls`
   写入并逐项读回核对。

## ESP 与 boot.img

`build-bundle.yml` 调用上游 `tools/package_release.py`，不自己拼 ESP。它负责
DTB 的 CPU model overlay、mkbootimg 合体 `boot.img`、固件要求的
`/EFI/Piano/stable/` 布局和逐文件读回。不传 `--root-size-mib`，root 走本仓库
的 F2FS 路径，不用上游的 ext4 分支。

## 产物尺寸（实测）

| 产物 | 未压缩 | 压缩后 | 状态 |
| --- | --- | --- | --- |
| Fedora 44 rootfs 树 | 5.2 GB | 1.90 GB | 已构建 |
| F2FS root 镜像（8192 MiB） | 8.00 GiB | 1.87 GiB | 已构建并核对 |

镜像核对结果：74825 个条目全部一致，xattr 逐项比对通过，fsck 退出码 0。

仓库为 public，artifact 与 Actions 分钟数不计费。

## 尚未验证

- 内核补丁后的重编结果未回。
- initramfs、F2FS 策略与 rootfs 载荷只在本机做过离线验证。
- ESP 与 boot.img 的完整流程未在 CI 跑过（依赖 mkbootimg 子模块）。
- 没有任何产物在设备上启动过。
