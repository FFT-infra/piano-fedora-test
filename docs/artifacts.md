# 部署产物

部署一台 piano 需要四样东西，来源分两类：SunUEFI 提供启动链与内核，本仓库提供 rootfs 与镜像。

| 产物 | 来源 | 本仓库状态 |
| --- | --- | --- |
| UEFI 固件（合体 BOOT） | SunUEFI `build.sh uefi` | 待接入 |
| 内核 Image + DTB + modules | SunUEFI `build.sh linux` | 待接入 |
| Fedora rootfs 树 | 本仓库 `build-rootfs.yml` | 已在 CI 构建 |
| F2FS root 镜像 | 本仓库 `build-image.yml` | 已实现 |
| ESP 镜像 | 本仓库 `build-esp-image.py` | 已实现 |

## 启动链与内核为什么待接入

SunUEFI 的构建链需要固定源码、EDK2 工具链和 ARM64 交叉编译环境，整链约 4~5 小时。它的公开 CI 当前在 Sensors 阶段失败（`mk-build-deps`），需要在我们的 workflow 里绕过 Sensors 只构建需要的部分。

接入方式：按 `sources.lock.json` 的 commit 取 SunUEFI，在我们的 workflow 里调用它的 `build.sh uefi` 与 `build.sh linux`，产物与本仓库的 rootfs、镜像汇总成部署包。

## 部署包应有的内容

| 文件 | 说明 |
| --- | --- |
| `esp.img` | FAT32，含 UEFI 启动文件与内核 |
| `pianoroot.f2fs.img` | F2FS root，label `PIANOROOT` |
| `boot.img` | 合体 BOOT，含 SunUEFI 选择器 |
| `manifest.json` | 各产物的 SHA256、容量、来源 commit |

## 产物尺寸

| 产物 | 未压缩 | 压缩后 |
| --- | --- | --- |
| Fedora rootfs 树 | 约 5.2 GB | 1.9 GB（`.tar.zst`） |
| F2FS root 镜像（8192 MiB） | 8 GiB 稀疏 | 视内容而定，大部分为空块 |

仓库为 public，artifact 与 Actions 分钟数不计费。

## 尚未验证

- UEFI 固件与内核尚未在本仓库构建过。
- F2FS 镜像尚未用真实 rootfs 制作过，只在 64 MiB fixture 上验证了工具正确性。
- 没有任何产物在设备上启动过。
