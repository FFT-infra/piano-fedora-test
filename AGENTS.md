# piano-fedora-test Agent Guide

本文件是这个仓库的权威约定。改仓库前先读它。

## 项目

小米平板 8 Pro（piano / 25091RP04C / SM8750）的 Fedora + HyperOS 双系统构建仓库，Linux 根分区用 F2FS。SunUEFI 按 commit 锁定为上游，本仓库只带 Fedora 后端、F2FS 全链路、首次分区三样增量。

## 仓库地图

- `sources.lock.json` — 上游仓库与 commit。唯一的上游真源。
- `product.json` — 要构建什么：发行版、文件系统、镜像尺寸。唯一的目标真源。
- `scripts/` — 构建脚本。`scripts/profiles/` 是发行版与桌面配方。
- `tests/` — 离线检查，`make test` 跑全部。
- `docs/architecture.md` — 组件边界与分阶段计划。
- `.github/workflows/` — 主机侧 CI，不接触设备。

## 规矩

- 上游不 fork。SunUEFI 按 `sources.lock.json` 的 commit 取用；本仓库的改动走自有脚本或补丁。
- 固件、校准数据、密钥不入库。`.gitignore` 默认拒绝。
- 写设备前必须有可恢复备份。设备操作只由人显式执行，CI 不碰设备。
- 证据分层。作者记录、构建通过、设备可用是三件事，不互相代替。
- 未定的值留 null 并让工具报错，不猜。`product.json` 里为 null 的字段就是待定项。
- 目录跟着实际交付走。没有文件就不建目录。

## 表达

- 先写结论，再写做法。不写"值得注意的是""由此可见""首先其次最后"。
- 不加粗打光。一段里最多一个粗体，且只在真需要时用。
- 一个事实只写一处，别处引用。

## 命令

```sh
make test                  # 全部离线检查
make plan RELEASEVER=44    # 打印 Fedora rootfs 构建计划
```

## 验证

- 改脚本 → `make test`。
- 改 workflow → 确认里面没有设备写入命令。
- 声称某件事通过时，附上本轮的命令与输出。
