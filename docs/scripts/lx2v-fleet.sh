#!/usr/bin/env bash

# lx2v-fleet.sh — 在 manager(238)上对所有 worker 节点批量执行 lx2v-node.sh 子命令。
#
# 读节点清单(默认 /root/lx2v-nodes.txt,一行一个内网 IP,# 注释与空行忽略),
# 逐台(并发)ssh 进去:先从 NFS 拉最新 lx2v-node.sh,再原样透传你给的参数执行。
# 依赖:238 已免密 ssh 到各节点(ssh-keygen + ssh-copy-id);各节点已挂 /nfs-models。
#
# 用法:
#   bash lx2v-fleet.sh upgrade-gpustack                          # 全体升 worker 镜像
#   bash lx2v-fleet.sh -j 3 upgrade-engine --engine lightx2v --offline   # 大 tar 降并发防 NFS 抢
#   bash lx2v-fleet.sh upgrade-engine --engine indextts --offline
#   bash lx2v-fleet.sh -j 3 upgrade-engine --engine bernini --offline    # 批量分发 bernini 引擎镜像
#   bash lx2v-fleet.sh -j 2 upgrade-engine --engine breeze --offline     # Breeze TTS 2(~26G,并发压到 2)
#   bash lx2v-fleet.sh status                                    # 全体巡检
#   bash lx2v-fleet.sh -f /path/other-nodes.txt <子命令...>       # 换清单
#   bash lx2v-fleet.sh --seq <子命令...>                          # 串行(= -j 1)
#
# 选项(必须放在 lx2v-node.sh 子命令之前):
#   -j N        并发数(默认 5)。engine --offline 建议 3,避免多台同时从 NFS load 10G tar 抢带宽;
#               breeze 的 tar 约 26G(是这批里最大的),建议再降到 2。
#   -f FILE     节点清单路径(默认 /root/lx2v-nodes.txt)。
#   --seq       串行执行。
#   --          显式结束选项解析,其后全部透传给 lx2v-node.sh。
#
# 退出码:任一节点失败则非 0;每台日志在 /tmp/lx2v-fleet/<ip>.log。
set -uo pipefail

NODES_FILE="${NODES_FILE:-/root/lx2v-nodes.txt}"
JOBS="${JOBS:-5}"
SELF_IP="${SELF_IP:-10.0.0.238}"          # manager 自身,清单里若混入则自动排除
NFS_SCRIPT="/nfs-models/_transfer/lx2v-node.sh"
LOCAL_SCRIPT="/root/lx2v-node.sh"
LOG_DIR="/tmp/lx2v-fleet"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

# --- 解析选项:-j/-f/--seq/-- 由本脚本消费,其余全部透传给 lx2v-node.sh ---
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -j) JOBS="$2"; shift 2 ;;
    -f) NODES_FILE="$2"; shift 2 ;;
    --seq) JOBS=1; shift ;;
    --) shift; ARGS+=("$@"); break ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
[ "${#ARGS[@]}" -gt 0 ] || { echo "用法: bash $0 [-j N] [-f nodes.txt] [--seq] <lx2v-node.sh 子命令与参数>"; exit 2; }
[ -f "$NODES_FILE" ] || { echo "✗ 节点清单不存在: $NODES_FILE(一行一个内网 IP)"; exit 2; }

# 读清单:去注释/空行,取每行第一列(容忍 "ip 备注"),排除 manager 自身
mapfile -t RAW_NODES < <(grep -vE '^[[:space:]]*(#|$)' "$NODES_FILE" | awk '{print $1}' | grep -vx "$SELF_IP")
[ "${#RAW_NODES[@]}" -gt 0 ] || { echo "✗ 清单里没有可用节点(全被注释/为空/只剩 manager)"; exit 2; }

# 查重。重复本身不致命(同一台跑两遍是幂等的),要报出来是因为它通常是**漏机器**的信号:
# 往清单尾部追加一批新节点时,若有几行误填成了已有 IP,总行数看着对、执行台数对、
# FAIL=0 也对 —— 三个数字全正常,却少覆盖了同样数量的节点。2026-08-23 的 lightx2v
# 引擎升级就这么漏了 5 台(清单 50 行/45 唯一,10.0.0.51/53/57/90/94 从未被派活),
# 直到超分任务落到未升级的节点上跑出 6 倍耗时才被发现。
mapfile -t DUP_NODES < <(printf '%s\n' "${RAW_NODES[@]}" | sort | uniq -d)
if [ "${#DUP_NODES[@]}" -gt 0 ]; then
  echo "⚠ 节点清单有重复($NODES_FILE):"
  for d in "${DUP_NODES[@]}"; do
    cnt=$(printf '%s\n' "${RAW_NODES[@]}" | grep -cx "$d")
    lines=$(grep -nE "^[[:space:]]*${d//./\\.}([[:space:]]|$)" "$NODES_FILE" | cut -d: -f1 | paste -sd, -)
    echo "    $d ×${cnt}  (行 ${lines:-?})"
  done
  echo "    → 已自动去重继续执行;但请核对清单:若它本应覆盖更多节点,多半是有行被误写成了已有 IP"
fi

# 去重,保留首次出现的顺序(不用 sort -u,以免打乱清单里有意安排的执行次序)
mapfile -t NODES < <(printf '%s\n' "${RAW_NODES[@]}" | awk '!seen[$0]++')

# 把 manager 上的 lx2v-node.sh 同步到 NFS,保证各节点拉到的是最新版
if [ -f "$LOCAL_SCRIPT" ]; then
  if ! cmp -s "$LOCAL_SCRIPT" "$NFS_SCRIPT" 2>/dev/null; then
    cp -f "$LOCAL_SCRIPT" "$NFS_SCRIPT" && echo "↻ 已把 $LOCAL_SCRIPT 同步到 $NFS_SCRIPT"
  fi
fi
[ -f "$NFS_SCRIPT" ] || { echo "✗ NFS 上没有 $NFS_SCRIPT(先在 238 跑 prepare-transfer 或把脚本放到该路径)"; exit 2; }

mkdir -p "$LOG_DIR"; rm -f "$LOG_DIR"/*.status 2>/dev/null || true

# 每台:拉最新脚本 + 执行透传参数。用 printf %q 逐个转义,防含空格/特殊字符的参数在远端被错解。
QUOTED_ARGS=$(printf '%q ' "${ARGS[@]}")
REMOTE_CMD="set -e; cp $NFS_SCRIPT /root/lx2v-node.sh; chmod +x /root/lx2v-node.sh; exec bash /root/lx2v-node.sh ${QUOTED_ARGS}"

DEDUP_NOTE=""
[ "${#DUP_NODES[@]}" -gt 0 ] && DEDUP_NOTE="(清单 ${#RAW_NODES[@]} 行,去重后 ${#NODES[@]})"
echo "==> 目标 ${#NODES[@]} 台 ${DEDUP_NOTE}· 并发 ${JOBS} · 命令: lx2v-node.sh ${ARGS[*]}"
echo "    日志目录: $LOG_DIR/<ip>.log"

run_one() { # run_one <ip> —— 后台任务,继承本 shell 的 SSH_OPTS/REMOTE_CMD 数组与变量
  local ip=$1
  local log="$LOG_DIR/$ip.log" st="$LOG_DIR/$ip.status"
  # REMOTE_CMD 是在 manager 上拼好、要在远端整体执行的命令串,故意远端展开(非本地)
  # shellcheck disable=SC2029
  if ssh "${SSH_OPTS[@]}" "root@$ip" "$REMOTE_CMD" > "$log" 2>&1; then
    echo OK > "$st";   echo "  ✅ $ip"
  else
    echo FAIL > "$st"; echo "  ❌ $ip — $(tail -n1 "$log" 2>/dev/null)"
  fi
}

# 并发调度:最多 JOBS 个同时在跑
running=0
for ip in "${NODES[@]}"; do
  run_one "$ip" &
  running=$((running + 1))
  if [ "$running" -ge "$JOBS" ]; then wait -n 2>/dev/null || wait; running=$((running - 1)); fi
done
wait

# 汇总
ok=0; fail=0; failed=()
for ip in "${NODES[@]}"; do
  if [ "$(cat "$LOG_DIR/$ip.status" 2>/dev/null)" = OK ]; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1)); failed+=("$ip")
  fi
done
echo "==> 完成: OK=${ok}  FAIL=${fail}  (共 ${#NODES[@]})"
if [ "$fail" -ne 0 ]; then
  echo "    失败节点: ${failed[*]}"
  echo "    排查: for ip in ${failed[*]}; do echo \"=== \$ip ===\"; tail -n20 $LOG_DIR/\$ip.log; done"
  exit 1
fi
echo "    全部成功。"
