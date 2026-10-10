# piano-fedora-test

小米平板 8 Pro（piano）的 Fedora + HyperOS 双系统构建仓库，Linux 根分区用 F2FS。

SunUEFI 按 commit 锁定为上游，本仓库只带三样增量：Fedora 后端、F2FS 全链路、首次分区。

Fedora 44/GNOME、A830v1 Mesa、F2FS/SELinux 内核与完整主机构建包已在 GitHub Actions 构建并核对。产物尚未真机验证，交付内容和构建顺序见 [产物说明](docs/artifacts.md)。

## 用法

```sh
make test                  # 离线检查
make plan RELEASEVER=44    # 打印 rootfs 构建计划
```

## 结构

| 路径 | 内容 |
| --- | --- |
| `product.json` | 要构建什么（发行版、文件系统、尺寸）。唯一目标真源 |
| `sources.lock.json` | 上游仓库与 commit。唯一上游真源 |
| `scripts/` | 构建脚本与发行版配方 |
| `tests/` | 离线检查 |
| `docs/architecture.md` | 组件边界与分阶段计划 |
| `docs/artifacts.md` | 部署需要哪些产物、各自来源 |

改动前先读 [AGENTS.md](AGENTS.md)。

## 许可

自有代码 MIT，见 [LICENSE](LICENSE)。上游依赖各自许可，见 `sources.lock.json`。
