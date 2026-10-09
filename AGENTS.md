# piano-fedora-test Agent Guide

本文件是权威约定。改仓库前先读它。

## 项目

小米平板 8 Pro（piano / 25091RP04C / SM8750）的 Fedora + HyperOS 双系统构建仓库，Linux 根分区用 F2FS。SunUEFI 按 commit 锁定为上游，本仓库只带 Fedora 后端、F2FS 全链路、首次分区三样增量。

## 仓库地图

每个事实只在一处定义，别处引用它。

| 路径 | 拥有的真源 | 说明 |
| --- | --- | --- |
| `sources.lock.json` | 上游是谁、锁定在哪个 commit | 只列实际消费的上游 |
| `product.json` | 要构建什么 | 发行版、文件系统、尺寸、分区标签 |
| `scripts/build-fedora-rootfs.py` | Fedora 后端 | plan 与 execute 两条路径 |
| `scripts/profiles/` | 发行版与桌面配方 | base 包、dnf 选项、桌面包组 |
| `tests/` | 离线检查 | `make test` 跑全部 |
| `docs/architecture.md` | 组件边界与阶段计划 | 分工、前提、本机约束 |
| `.github/workflows/` | 主机侧 CI | 不接触设备 |

## 硬规则

- 上游不 fork。SunUEFI 按 `sources.lock.json` 的 commit 取用；本仓库的改动走自有脚本或补丁。
- 固件、校准数据、密钥不入库。
- 证据分层。作者记录、构建通过、设备可用是三件事，不互相代替。
- 未定的值留 null 并让工具报错，不猜。`product.json` 里的 null 就是待定项。
- 目录跟着实际交付走。没有文件就不建目录。
- 没有生成文件。若将来引入，源与产物分开，产物不入库。

## 命令

```sh
make test                  # 全部离线检查
make plan RELEASEVER=44    # 打印 Fedora rootfs 构建计划
```

## 验证

| 改了什么 | 跑什么 |
| --- | --- |
| `scripts/` | `make test` |
| `product.json`、`sources.lock.json` | `make test` |
| `docs/`、`README.md` | `make test`（含链接检查） |
| `.github/workflows/` | `make test` |

声称某件事通过时，附上本轮的命令与输出。没跑的检查标为未跑，不写成通过。

## 提交

格式 `{type}: {description}`，type 取 `feat`、`fix`、`refactor`、`docs`、`chore`。一个提交一件事，不混。

## 表达

仓库文档用中文。

- 先写结论，再写做法。
- 不加粗打光，一段最多一个粗体。
- 不写"值得注意的是""由此可见""首先其次最后""不是 A 而是 B"。
- 一个事实只写一处，别处引用。
