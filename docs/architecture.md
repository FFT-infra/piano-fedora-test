# 架构

SunUEFI 提供启动链，本仓库加三样：Fedora 后端、F2FS 全链路、首次分区。

## 分工

| 层 | 归属 |
| --- | --- |
| 合体 BOOT、UEFI、内核编排、打包、安装器 | SunUEFI（锁定） |
| Fedora rootfs 后端与配方 | 本仓库 |
| F2FS 全链路（内核策略、initramfs、fstab、udev、镜像、安装器） | 本仓库 |
| 首次分区（上游返回 `NEW_INSTALL_NOT_READY`） | 本仓库 |

上游不 fork，改动走自有脚本或补丁。

## 阶段

每阶段独立可用，都在主机上完成，不碰设备。

1. Fedora 后端。给上游加 dnf5 路径，产出 Fedora rootfs 树。参考 [fedora-sheng](https://github.com/mumuxiao722/fedora-sheng) 的 RPM spec 与 Actions 参数化，不迁移它的 SM8550 硬件内容与 mkbootimg 启动链。
2. F2FS 全链路。涉及六处 ext4 硬编码：内核配置策略、`release-disk-bootstrap`、fstab、udev 放行、`package_release.py` 的镜像制作、安装器 magic 与 SSH 注入。

   制镜像不用 `sload -P`。上游 f2fs-tools 的 sload 只保留 owner/mode，不枚举源树的 xattr/ACL/capability，用它会在导入阶段静默丢元数据。改为挂载真实 F2FS 后用 `tar --xattrs --acls --numeric-owner` 写入，再挂载读回逐项比对。

   制镜像需要挂载 F2FS。GitHub runner 的内核默认不带 f2fs，装上 `linux-modules-extra` 后 `modprobe f2fs` 可用，挂载式往返验证在 CI 上已跑通（`IMAGE_VERIFIED`，xattr 核对通过，fsck 退出码 0）。

   离线导入 `sload.f2fs -P` 不需要挂载，但它只保留 owner 与 setuid，**不保留 user xattr**，已在 192.168.5.11 实测确认。所以制镜像用挂载 + `tar --xattrs --acls`，不用 sload。

## CI 分工

| workflow | 触发 | 产出 |
| --- | --- | --- |
| `host-check.yml` | push / PR | 离线检查，`make test` |
| `build-rootfs.yml` | 手动 | Fedora rootfs 树 + `.tar.zst`，上传 artifact |
| `build-kernel.yml` | 手动 | F2FS/SELinux Image、完整模块与匹配 SDK |
| `build-uefi.yml` | 手动 | UEFI 产品与原始 manifest |
| `build-mesa.yml` | 手动 | Fedora 原生 A830v1 Mesa RPM 与实际驱动检查记录 |
| `build-device-runtime.yml` | 手动 | Fedora 原生设备运行时与匹配内核 ABI 的外部模块 |
| `build-initramfs.yml` | 手动 | 独立 initramfs 构建与检查；正式 bundle 消费 build-image 的 initramfs |
| `verify-f2fs.yml` | push / PR | 小 fixture 的 F2FS 元数据往返验证 |
| `build-image.yml` | 手动 | 同一次集成的设备 rootfs、initramfs、F2FS 镜像及相互绑定的 manifest |
| `build-bundle.yml` | 手动 | 汇总内核、UEFI、rootfs、镜像为部署包 |

产物由各 workflow 显式上传，上传失败不计为交付完成。

产物清单与部署包结构见 [部署产物](artifacts.md)。
3. 首次分区。只读探测本机并生成计划，用模拟 GPT 在主机上验证。
4. 安装与验收。实际写入，启动验证，双向切换。

F2FS 排在分区之前：F2FS 是技术风险（可能走不通），分区是工程风险（设计对了就能做），且分区方案依赖 root 镜像容量。

## 前提

整套方案假设 SunUEFI 的启动交接在本机成立。上游验证机是 16GB / CSOT / OS3.0.309，本机是 24GB / 1TB / OS3.0.305。若不成立，阶段 3 的只读探测会先暴露，此时转向容器路线，不继续投入分区工作。

## 本机约束

1. 扩容机，规格高于官方，分区与校准可能被第三方改过。
2. 全盘无空位，`userdata` 占满盘尾。
3. `/data` 是加密 dm 映射，上游无损缩容的前提不成立。
4. 无 `/dev/kvm`，`kvm-arm.mode=protected`，虚拟化只能走 AVF/crosvm + Gunyah。

## 许可

自有代码 MIT。SunUEFI 为文件级许可，内核 GPL-2.0，固件许可不明且不入库。上游依赖按各自许可取用，不复制。
