# 补丁

上游按 commit 锁定，改动走这里的补丁，不 fork。

## kernel-f2fs

让内核内建 F2FS 支持，这是 F2FS 根分区能挂载的前提。

上游 `build_piano_full_kernel.py` 的配置策略只允许三个已审阅的覆盖
（Bluetooth UHID、flash、early-CPUCP），其余一律拒绝：

    Local full fragment may override only CONFIG_CMDLINE
    Only reviewed Bluetooth/flash/early-CPUCP configuration overrides are supported

所以不能往 `linux/configs/piano-full.config` 里追加 `CONFIG_F2FS_FS=y`，
也不能只把 rootfstype 改成 f2fs——两条路都会在配置校验阶段被拒。

`kernel-f2fs/` 里的补丁按上游自己的扩展点做三件事：

1. 新增一份可审阅的 F2FS 配置片段 `linux/configs/piano-f2fs.config`。
2. 在 `ALLOWED_OVERRIDES` 里登记这组选项，与 flash/early-CPUCP 同构。
3. 在 `build_piano_full_kernel.py` 里加载它，并让 `REQUIRED` 校验覆盖。

补丁针对 `sources.lock.json` 里锁定的 SunUEFI commit。上游更新后补丁可能
不再适用，`apply-patches.py` 会在应用前核对目标文件摘要，不匹配就报错退出。

## 应用方式

```sh
scripts/apply-patches.py --sunuefi DIR
```

工具按 `series.json` 的顺序应用，逐个核对上下文，失败即停并说明是哪个补丁、
哪个文件、哪一行。CI 的 `build-kernel.yml` 在构建前调用它。

## 选项为什么是这几个

`CONFIG_F2FS_FS=y` 内建，不是模块。根分区在 initramfs 阶段就要挂载，模块
形式会让早期挂载依赖模块加载顺序，而 F2FS 正是被挂载的那个文件系统。

XATTR / POSIX_ACL / SECURITY 三项对应镜像制作时用 `tar --xattrs --acls`
写入的元数据。少任何一项，Fedora rootfs 的 capability 或 ACL 就会在挂载后
读不回来。这三项与 sheng 的 `sm8550.config` 一致，来源见
`sources/sheng-Reference/mipad-6s-pro-linux/sm8550.config`。

`CONFIG_F2FS_CHECK_FS` 不开：它是运行时一致性检查，代价是性能，镜像制作阶段
已用 `fsck.f2fs` 单独校验。

`CONFIG_F2FS_FS_COMPRESSION` 不开：首版不需要，开了会改变磁盘格式特性，
让镜像与未开启该特性的内核不兼容。

## uefi-ufs

修复 SunUEFI 在 1TB UFS 设备（HyperOS 3.0.305）上冷启动或从 Android 重启时陷入休眠死循环的缺陷。

### 现象与根因

上游 `PianoUfsReadOnlyDma.c` 在 UniPro 链路从 Hibern8 恢复后，仅在“设备描述符读取返回 `EFI_DEVICE_ERROR`”时才向 WLUN `0xD0` 发送 `START_STOP_UNIT` 唤醒 UFS 设备。在 1TB UFS 闪存上，描述符在 Sleep 态（`0x22`/`0x33`）下依然可被成功读取，导致上游完全跳过 `ResumeActive`。紧接着执行的 SCSI `REPORT_LUNS` 因 UFS 设备尚未就绪返回 `CHECK CONDITION: NOT READY (key=02 asc=04 ascq=00)`，且没有任何延时重试，直接触发 `CpuDeadLoop()` 挂起。

### 补丁改动

1. 无论描述符是否读取成功，始终核对当前电源模式；若非 Active（`0x11`），强制发起 `PianoUfsBuildResumeActive` 唤醒设备。
2. 为 `REPORT_LUNS` 增加 5 次有界重试，单次延时 50ms 并重试唤醒。
3. 为 `READ_CAPACITY_16` 增加 5 次有界重试，单次延时 20ms。

