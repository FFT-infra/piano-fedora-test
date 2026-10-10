# 首次安装与验收

安装材料由 `prepare-install.yml` 生成，实际操作由连接平板的 Linux 主机执行。
Actions 不连接平板。构建成功、传输成功、Linux 启动成功分别记录。

## 分区与容量

`product.json` 记录本机已核对的目标容量。规划器默认保留 Android 固定分区，
将原 userdata 扣除 ESP 后的空间对半分；1 MiB 对齐允许两边相差不超过 2 MiB。
执行器从实时、双副本 CRC 合格的完整 GPT 重算，结果必须与镜像容量一致。
原 userdata 会清空重建，GPT/BOOT 恢复文件不包含 Android 用户数据。

通用包中的 8 GiB F2FS 镜像保持封存。`prepare-root-capacity.py` 在私有工作副本中
设置本机启动配置、传感器槽位和 root/piano SSH 公钥，恢复匹配的 Fedora SELinux
标签后离线扩容。源树与扩容后的文件、所有权、权限、链接、mtime、全部 xattr
逐项比较。只读 fsck 和 sparse 解码摘要必须通过。

sparse 文件使用 RAW 与显式零 FILL，不使用 DONT_CARE；其完整展开字节与
验证过的 F2FS 镜像一致。它仍是 F2FS 文件系统，sparse 只负责 Fastboot 传输。

## Actions 材料

手动运行 `prepare-install`，选择完整 deploy-bundle 的 run、当前 Android 槽位，
并提供本次安装管理用的 Ed25519 公钥。Linux 容量留空时使用 `product.json`。
私钥不传入 Actions。三个 artifact 分别为：

| artifact | 内容 |
| --- | --- |
| root-install | 扩容并配置后的 F2FS sparse、容量/元数据校验记录、摘要 |
| boot-tools | 同核心的 selector、Android 静态原生工具、产品固件、自包含安装入口 |
| host-boot-tool | 静态宿主 BOOT 重打包工具及来源记录 |

boot-tools 必须与所选 deploy-bundle 的 UEFI 产品镜像逐字节相同。
安装入口位于 boot-tools 的 `installer/scripts`；其中的锁定上游代码带源码清单，
不需要在设备主机重新编译。普通发行版主机需要 Python 3、adb、fastboot；
BOOТ 准备使用 Actions 提供的宿主工具。

## 本机 BOOT

先确认当前原 BOOT 完整摘要、槽位和系统版本。`prepare-device-boot.py` 使用
这些明确输入和同核心的 Actions 工具生成 `boot_android.img`、`boot_linux.img`
及 `boot-request.json`。普通 Android 为默认目标。

包装后恢复必须得到完整原 BOOT 的同一 SHA256。Linux 持久请求、只读预览、
改回 Android 后的再次完整恢复都要成功。文件检查不替代真机旁路和 Recovery 验收。

使用 `python3 scripts/prepare-device-boot.py --help` 查看文件参数。
原 BOOT、生成的设备 BOOT、安装记录和私钥均留在本地，不进入代码仓库。

## 实际首次安装

使用 `python3 scripts/install-f2fs.py --help` 查看入口。默认 `plan` 只读设备并
记录身份、完整 GPT 基线、当前原 BOOT 与三个输入包的身份。
`apply` 必须显式指定 `--execute --accept-data-loss`、已复核计划和新的会话目录。

执行顺序为：

1. 重新读取设备，确认计划、槽位、原 BOOT 和输入包未变，保存原 GPT/BOOT 恢复材料。
2. 临时运行产品 UEFI 并核对 USB 协议，再返回同一 Android；临时运行合体 BOOT 的
   Android 旁路，也必须返回且持久 BOOT/GPT 未变。
3. 使用 Android 已有 sgdisk 写入按完整二进制 GPT 生成的布局，读回两份 header/table
   并验证 CRC、所有分区记录和固定 Android 分区字节。
4. 进入原厂 Fastboot，确认新 userdata/root/ESP 容量；擦除 userdata，再传输 ESP、
   F2FS sparse 和当前槽的 Android 默认合体 BOOT。
5. 保持 Fastboot，记录 `IMAGES_TRANSFERRED_FIRST_BOOT_PENDING`。传输成功不能写成
   文件系统读回成功、Android 初始化成功或 Linux 启动成功。

Android 的 userdata 重建使用该 ROM 自身的 fs_mgr；计划阶段必须确认其 `/data`
条目具有 formattable 合同。不能直接在宿主格式化 Android 的加密映射。

会话的 result.json 记录每个已完成阶段和错误。GPT 写入或擦除后发生中断时，
先看记录与实际设备状态，不自动重试、恢复旧 GPT 或启动 Android。
恢复 GPT 不能恢复已擦除的 Android 文件。

## 真机验收

首次 Linux 启动后检查 F2FS 容量、匹配内核/模块、GNOME 会话和日志；随后验收
Android 对新 userdata 的初始化，以及 Android→Linux、Linux→Android 和原厂
Recovery。触屏、显示、网络、音频、传感器和充电需要单独的实际行为证据。
这些项目未验收前，设备可用状态保持未验证。
