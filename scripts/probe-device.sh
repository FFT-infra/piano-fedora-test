#!/usr/bin/env bash
# 只读探测 piano：ADB 取身份、槽位、GPT、分区表。
#
# 只做查询。没有任何写操作，不 flash、不 dd、不改 GPT。
#
# 用法：
#   scripts/probe-device.sh --host 192.168.5.11 [--out DIR]
#
# 输出：DIR 下若干文本文件，可直接喂给 plan-first-partition.py。

set -euo pipefail

HOST=""
OUT=""

while (($#)); do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --out)  OUT="$2";  shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$HOST" ]]; then
  echo "ERROR: --host is required" >&2
  exit 2
fi

if [[ -z "$OUT" ]]; then
  OUT="$(mktemp -d)"
fi
mkdir -p "$OUT"

echo "probing $HOST, output to $OUT"

# 把远端命令集中在一个函数里，避免多处引号嵌套出错。
# ssh 的退出码要显式处理：adb shell 在无匹配 grep 时会返回非零，
# 不能让 set -e 把整个探测中断。
remote() {
  ssh -o ConnectTimeout=10 "$HOST" "$1" || return 0
}

# 1. 设备是否在线。
remote 'adb devices -l' > "$OUT/adb-devices.txt" 2>&1
if ! grep -qE '\bdevice\b' "$OUT/adb-devices.txt"; then
  echo "ERROR: no adb device attached on $HOST" >&2
  cat "$OUT/adb-devices.txt" >&2
  exit 1
fi

# 2. 身份与启动状态。
remote 'adb shell getprop | grep -E "ro.product.(model|device|marketname)|ro.build.version.(release|incremental)|ro.boot.(slot_suffix|verifiedbootstate|flash.locked)|ro.crypto.(state|type)|ro.boot.hardware.cpu.pagesize"' \
  > "$OUT/props.txt" 2>&1

# 3. GPT 与分区表（只读）。
remote 'adb shell su -c "sgdisk --print /dev/block/sda"' > "$OUT/gpt.txt" 2>&1

# 4. 分区名到块设备的映射。
remote 'adb shell su -c "ls -l /dev/block/by-name/"' > "$OUT/by-name.txt" 2>&1

# 5. 容量与扇区。
remote 'adb shell su -c "cat /sys/class/block/sda/size; cat /sys/block/sda/queue/logical_block_size; cat /sys/block/sda/queue/physical_block_size"' \
  > "$OUT/geometry.txt" 2>&1

# 6. 挂载状态（看 userdata 是否走 dm）。grep 无匹配时返回 1，不能让它中断脚本。
remote 'adb shell su -c "mount | grep -E \" /data | /metadata \""' > "$OUT/mounts.txt" 2>&1 || true

# 7. 用真实 GPT 跑一次只读可行性检查。
python3 "$(dirname "$0")/plan-first-partition.py" --dump "$OUT/gpt.txt" --inspect-only \
  > "$OUT/inspect.json" 2>&1 || echo "inspect failed; see $OUT/inspect.json"

echo "done. files:"
ls -1 "$OUT"
echo
echo "next: python3 scripts/plan-first-partition.py --dump $OUT/gpt.txt --esp-mib 512 --root-mib 16384"
