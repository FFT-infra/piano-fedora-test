# 构建与交付产物

目标是 Fedora 44/GNOME + F2FS 的主机构建集合。平板启动、首次分区和本机 BOOT 包装需要另行验收；构建阶段停在设备写入之前。

| 产物 | 生产入口 | 集成关系 |
| --- | --- | --- |
| UEFI 产品 | build-uefi.yml | 使用固定 SunUEFI 配方；保留原始 manifest |
| Image、完整模块、SDK | build-kernel.yml | 内建 F2FS/SELinux，强制 rootfstype=f2fs；SDK 绑定同一配置和 Module.symvers |
| Fedora 基础树 | build-rootfs.yml | 原生 DNF installroot，记录容器 ID 和实际 RPM，排除发行版 kernel |
| A830v1 Mesa RPM | build-mesa.yml | 固定 Fedora 源 RPM 与 piano 补丁，复用 Fedora 配方；从实际 RPM 核对 Gallium/Turnip 芯片表 |
| 设备运行时 RPM | build-device-runtime.yml | Fedora 原生触控/笔、相机程序、传感器、BOOT 请求工具、拓扑与设备配置；外部模块匹配 SDK |
| 设备 rootfs、initramfs、F2FS raw 镜像 | build-image.yml | 同一 job 安装设备与 Mesa RPM，运行上游完整 rootfs 检查，配置服务、清理身份、设置 SELinux 标签并完整读回验证 |
| ESP、Linux boot.img、交付集合 | build-bundle.yml | 从集成记录取得准确内核，调用上游 DTB/mkbootimg/ESP 包装，核对全部文件和展开 root 镜像 |

## 一致性要求

每份 initramfs、运行时和最终 rootfs manifest 都引用完整 kernel manifest 的 SHA256。kernel release 相同不足以证明配置相同；最终组合必须匹配这份摘要。

F2FS 镜像同时绑定 rootfs、内核、运行时、Mesa 和 initramfs 五份清单的 SHA256。rootfs 清单引用实际消费的运行时与 Mesa 清单，并记录实际 RPM 清单的摘要。

组件分目录解包。基础树、内核、UEFI、运行时、Mesa 和 initramfs 不共享 manifest 路径。跨 run 查找仅接受成功且未过期的 artifact；SDK 与内核从同一次 run 获取。镜像产物中的 source-runs.json 决定 bundle 使用哪个内核。

## F2FS 验证

导入使用 GNU tar 的 pax、全部 xattr namespace、ACL、numeric owner 与 SELinux 恢复选项。逐条核对文件内容、UID/GID、mode、mtime、软链接目标、硬链接关系、全部 xattr、capability 与目标 SELinux 标签。属性读取错误会失败，目录大小不参与跨文件系统比较。

镜像先卸载，再运行 `fsck.f2fs -f --dry-run`，并核对检查前后镜像摘要不变。随后以 `ro,norecovery` 挂载读回，卸载后才生成最终镜像摘要。仅有 fsck 退出 0 或小 fixture 通过不证明完整交付通过。

容量是构建参数。当前 8192 MiB 是测试镜像容量，product.json 的最终分区容量仍未选定；没有 F2FS 自动扩容承诺，也没有对 userdata 的写入。

## 交付内容

最终 deploy-bundle 包含 esp.img、Linux boot.img、PianoUEFI-product.img、pianoroot.f2fs.img.zst、对应 manifest、source-runs.json 与 SHA256SUMS。verify-bundle.py 会流式展开压缩 root，核对实际 raw 大小和摘要，再记录 HOST_VERIFIED_FEDORA_F2FS_BUNDLE_NOT_DEVICE_VERIFIED。

Linux boot.img 是 ESP 上的 Linux 载荷。包含当前 Android 内核的合体 BOOT 必须用本机当前 stock BOOT、槽位和 generation 另行包装；通用构建产物不包含这些个人设备输入。

## 参考来源

现有研究工作区的 architecture、planning-verification、source-deep-dive、sheng-f2fs-reference 和各类仓库比较用于限定实现范围。新增阅读 code002-2/piano-linux 的 AGENTS.md，固定快照 f1589d9b499a44998fd97bfbd500789d14622397；它的组件分层与源身份记录可参考，Debian/ABL 路线和仓库专属流程不替代本项目的 Fedora/F2FS/SunUEFI 约定。

源码直接依赖仍以 sources.lock.json 为准；参考文档不会自动进入生产依赖。所有下载与生成文件在被忽略的 build/、out/，固件、校准和密钥不进入代码仓库。

## 尚需验证

2026-10-10 的完整 Actions 构建及下载后的本地校验已通过，包括组件清单摘要、压缩 root 展开后的 8 GiB 大小与 SHA256，以及 ESP 载荷和 boot.img 组件。没有任何本次产物在平板上启动过，首次分区、恢复材料、本机 BOOT 输入和 Android 数据处理选择仍属于部署前的独立要求。

安装适配尚未完成：上游安装器仍按 ext4 包结构处理 root，本仓库尚未提供消费这份 F2FS 交付包的安装入口。首次分区工具当前只生成计划，实际初装与本机 BOOT 包装还需在分区容量和 Android 数据处理方案确定后完成。
