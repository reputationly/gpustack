#!/usr/bin/env bash
# lx2v-node.sh — LightX2V/GPUStack GPU 节点一键安装与升级(fork 运维脚本)
#
# 用法(在 GPU 节点上以 root 执行):
#   ./lx2v-node.sh install --token <GPUSTACK_TOKEN> [--worker-ip <IP>] [--offline]
#   ./lx2v-node.sh upgrade-gpustack [--offline]     # 换 gpustack:lx2v-dev 并原参数重启 worker
#   ./lx2v-node.sh upgrade-engine   [--offline]     # 换 lightx2v 引擎镜像(实例需重建才生效)
#   ./lx2v-node.sh status                            # 节点健康速览
#   ./lx2v-node.sh prepare-transfer                  # (238/有 ACR 外网的机器)拉镜像存 NFS tar
#
# --offline:不走 ACR,直接从 NFS 的 _transfer/ tar docker load(全新节点/网络受限时用)。
# 进度可视:每步打印 [step i/N] 开始时间与耗时;长任务(load/pull/save)原生输出直通;
# 全程 tee 到 /var/log/lx2v-node-<日期>.log。
#
# 对应 runbook:docs/lightx2v-gpustack-部署实录.md §12.1/§17.7 与
# docs/lightx2v-20260706-发布部署验证全记录.md §4。
set -euo pipefail

# ---------- 可调配置 ----------
REGISTRY="crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly"
GPUSTACK_IMAGE="${REGISTRY}/gpustack:lx2v-dev"
ENGINE_IMAGE="${REGISTRY}/lightx2v:arm64-a100-latest"
SERVER_URL="${SERVER_URL:-http://10.0.0.238}"
NFS_SERVER="100.125.40.2"
NFS_MODELS_EXPORT="/share-LLM"
NFS_OUTPUT_EXPORT="/share-output"
TRANSFER_DIR="/nfs-models/_transfer"
GPUSTACK_TAR="${TRANSFER_DIR}/gpustack-lx2v-dev-arm64.tar"
ENGINE_TAR="${TRANSFER_DIR}/lightx2v-arm64-profiles.tar"
NVIDIA_REPO_DIR="${TRANSFER_DIR}/nvidia-repo"
WORKER_NAME="gpustack-worker"
WORKER_PORT=10150
# ------------------------------

LOG_FILE="/var/log/lx2v-node-$(date +%Y%m%d).log"
exec > >(tee -a "$LOG_FILE") 2>&1

STEP_NO=0
STEP_TOTAL=0
STEP_T0=0
step() {
  [ "$STEP_NO" -gt 0 ] && echo "    ... 上一步耗时 $((SECONDS - STEP_T0))s"
  STEP_NO=$((STEP_NO + 1))
  STEP_T0=$SECONDS
  echo ""
  echo "==> [step ${STEP_NO}/${STEP_TOTAL}] $(date '+%H:%M:%S')  $*"
}
finish() {
  [ "$STEP_NO" -gt 0 ] && echo "    ... 上一步耗时 $((SECONDS - STEP_T0))s"
  echo ""
  echo "==> 完成:总耗时 $((SECONDS / 60))m$((SECONDS % 60))s  (日志: ${LOG_FILE})"
}
die() { echo "!! 失败: $*" >&2; exit 1; }

# 后台任务的文件大小进度条(docker save 无原生进度)
watch_size() { # watch_size <pid> <file>
  local pid=$1 file=$2
  while kill -0 "$pid" 2>/dev/null; do
    sleep 10
    [ -f "$file" ] && echo "    ... $(date '+%H:%M:%S') $(du -h "$file" 2>/dev/null | cut -f1) 已写入"
  done
}

detect_worker_ip() {
  # 取 10.x 网段第一个地址;可用 --worker-ip 覆盖
  hostname -I | tr ' ' '\n' | grep -E '^10\.' | head -1
}

parse_flags() {
  TOKEN="${GPUSTACK_TOKEN:-}"
  WORKER_IP=""
  OFFLINE=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --token) TOKEN="$2"; shift 2 ;;
      --worker-ip) WORKER_IP="$2"; shift 2 ;;
      --offline) OFFLINE=1; shift ;;
      *) die "未知参数: $1" ;;
    esac
  done
  [ -n "$WORKER_IP" ] || WORKER_IP="$(detect_worker_ip)"
}

fetch_image() { # fetch_image <image> <tar>
  local image=$1 tar=$2
  if [ "$OFFLINE" -eq 1 ]; then
    [ -f "$tar" ] || die "offline 模式但 tar 不存在: $tar(先在 238 跑 prepare-transfer)"
    echo "    从 NFS load: $tar ($(du -h "$tar" | cut -f1))"
    docker load -i "$tar"
  else
    # 有旧镜像时 pull 只拉增量层;失败自动回退 NFS tar
    if ! docker pull "$image"; then
      echo "    pull 失败,回退 NFS tar ..."
      [ -f "$tar" ] || die "pull 失败且无 NFS tar 可用: $tar"
      docker load -i "$tar"
    fi
  fi
  echo "    当前镜像: $(docker images --format '{{.ID}}  {{.Repository}}:{{.Tag}}' | grep -F "${image#*/}" | head -1)"
}

# 全新节点装机用:NFS tar 在就 load(内网快),不在则在线 pull
fetch_image_prefer_tar() { # fetch_image_prefer_tar <image> <tar>
  local image=$1 tar=$2
  if [ -f "$tar" ]; then
    echo "    从 NFS load: $tar ($(du -h "$tar" | cut -f1))"
    docker load -i "$tar"
  else
    echo "    NFS tar 不存在,在线拉取 ${image} ..."
    docker pull "$image" || die "pull 失败且无 NFS tar: $tar"
  fi
  echo "    当前镜像: $(docker images --format '{{.ID}}  {{.Repository}}:{{.Tag}}' | grep -F "${image#*/}" | head -1)"
}

ensure_nfs() {
  if ! mountpoint -q /nfs-models; then
    grep -q "$NFS_MODELS_EXPORT" /etc/fstab || cat >> /etc/fstab <<EOF
${NFS_SERVER}:${NFS_MODELS_EXPORT} /nfs-models nfs rw,hard,nolock,noresvport,_netdev 0 0
${NFS_SERVER}:${NFS_OUTPUT_EXPORT} /nfs-output nfs rw,hard,nolock,noresvport,_netdev 0 0
EOF
    mkdir -p /nfs-models /nfs-output
    mount -a
  fi
  mountpoint -q /nfs-models || die "/nfs-models 挂载失败"
  mountpoint -q /nfs-output || die "/nfs-output 挂载失败"
  ln -sf /nfs-models/wuhanjisuan894 /nfs-data
  ln -sf /nfs-models/wuhanjisuan894 /data
  ls /nfs-models/wuhanjisuan894/models/ > /dev/null || die "NFS 内容不可读"
  echo "    NFS OK(/nfs-models + /nfs-output + 软链)"
}

current_worker_volume() {
  docker inspect "$WORKER_NAME" --format \
    '{{range .Mounts}}{{if eq .Destination "/var/lib/gpustack"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || true
}

current_worker_token() {
  docker inspect "$WORKER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep '^GPUSTACK_TOKEN=' | cut -d= -f2- || true
}

run_worker() { # run_worker <token> <worker_ip> <volume>
  local token=$1 ip=$2 volume=$3
  docker run -d --name "$WORKER_NAME" \
    -e "GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME=${WORKER_NAME}" \
    -e "GPUSTACK_TOKEN=${token}" \
    -e "GPUSTACK_EXTRA_MOUNTS=/nfs-models,/nfs-output" \
    --restart=unless-stopped --privileged --network=host \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --volume "${volume}:/var/lib/gpustack" \
    --volume /nfs-models:/nfs-models --volume /nfs-output:/nfs-output \
    --runtime nvidia \
    "$GPUSTACK_IMAGE" \
    --server-url "$SERVER_URL" --worker-ip "$ip"
}

verify_worker() {
  local i
  for i in $(seq 1 30); do
    if docker logs "$WORKER_NAME" 2>&1 | grep -q "registered with worker_id"; then
      docker logs "$WORKER_NAME" 2>&1 | grep -E "Registering|registered" | tail -2
      break
    fi
    sleep 2
    [ "$i" -eq 30 ] && { docker logs "$WORKER_NAME" 2>&1 | tail -10; die "worker 60s 内未注册成功"; }
  done
  curl -sf --max-time 3 "http://127.0.0.1:${WORKER_PORT}/healthz" > /dev/null \
    || die "本机 healthz 不通"
  echo "    本机 healthz OK。⚠️ 若 UI 不转 Ready:检查云安全组(须与既有节点同组,"
  echo "    症状=server ping 通但 TCP ${WORKER_PORT} 超时,见全记录 §4.2 坑 C)"
}

cmd_install() {
  parse_flags "$@"
  [ -n "$TOKEN" ] || die "install 需要 --token(在既有 worker 上: docker inspect ${WORKER_NAME} | grep GPUSTACK_TOKEN)"
  STEP_TOTAL=7

  step "预检:GPU 驱动 / 架构"
  nvidia-smi -L || die "nvidia-smi 不可用,先装 GPU 驱动"
  [ "$(uname -m)" = "aarch64" ] || echo "    ⚠️ 非 arm64 机器,镜像 tar 是 arm64 的"

  step "apt 基础包(逐个装,避免一包失败全中止)"
  apt-get update -q
  apt-get install -y -q docker.io
  apt-get install -y -q nfs-common

  step "挂载 NFS + 软链"
  ensure_nfs

  step "nvidia-container-toolkit(源自 NFS ${NVIDIA_REPO_DIR})"
  if ! command -v nvidia-ctk > /dev/null; then
    [ -d "$NVIDIA_REPO_DIR" ] || die "缺 ${NVIDIA_REPO_DIR}(在既有节点: cp /etc/apt/sources.list.d/nvidia-container-toolkit.list 与 keyring 到该目录)"
    cp "${NVIDIA_REPO_DIR}/nvidia-container-toolkit-keyring.gpg" /usr/share/keyrings/
    cp "${NVIDIA_REPO_DIR}/nvidia-container-toolkit.list" /etc/apt/sources.list.d/
    apt-get update -q && apt-get install -y -q nvidia-container-toolkit
  fi
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  docker info 2>/dev/null | grep -q "nvidia" || die "docker nvidia runtime 未生效"

  step "镜像:gpustack(NFS tar 优先,无则在线拉)"
  fetch_image_prefer_tar "$GPUSTACK_IMAGE" "$GPUSTACK_TAR"

  step "镜像:lightx2v 引擎(NFS tar 优先,无则在线拉)"
  fetch_image_prefer_tar "$ENGINE_IMAGE" "$ENGINE_TAR"

  step "起 worker 并验证注册(worker-ip=${WORKER_IP})"
  docker rm -f "$WORKER_NAME" 2>/dev/null || true
  run_worker "$TOKEN" "$WORKER_IP" "gpustack-data"
  verify_worker
  finish
}

cmd_upgrade_gpustack() {
  parse_flags "$@"
  STEP_TOTAL=4
  docker inspect "$WORKER_NAME" > /dev/null 2>&1 || die "本机没有 ${WORKER_NAME} 容器(全新节点请用 install)"

  step "读取现有 worker 配置(token/卷/IP 原样保留)"
  local token volume
  token="$(current_worker_token)"
  volume="$(current_worker_volume)"
  [ -n "$token" ] || die "读不到 GPUSTACK_TOKEN"
  [ -n "$volume" ] || die "读不到数据卷名(匿名卷也会有 64 位卷名;若为空说明容器无卷,危险,停止)"
  echo "    volume=${volume}  worker-ip=${WORKER_IP}"

  step "拉取/加载新 gpustack 镜像"
  fetch_image "$GPUSTACK_IMAGE" "$GPUSTACK_TAR"

  step "重建 worker 容器"
  docker stop "$WORKER_NAME" && docker rm "$WORKER_NAME"
  run_worker "$token" "$WORKER_IP" "$volume"

  step "验证注册"
  verify_worker
  finish
}

cmd_upgrade_engine() {
  parse_flags "$@"
  STEP_TOTAL=2
  step "当前引擎镜像"
  local old_id
  old_id="$(docker images --format '{{.ID}}' "$ENGINE_IMAGE" | head -1 || true)"
  echo "    old=${old_id:-<无>}"

  step "拉取/加载新引擎镜像"
  fetch_image "$ENGINE_IMAGE" "$ENGINE_TAR"
  local new_id
  new_id="$(docker images --format '{{.ID}}' "$ENGINE_IMAGE" | head -1)"
  if [ "$old_id" = "$new_id" ]; then
    echo "    镜像未变化(已是最新)"
  else
    echo "    ${old_id:-<无>} -> ${new_id}"
    echo "    ⚠️ 正在运行的实例仍用旧镜像;到 UI 逐个删除实例让其自动重建即可生效"
    echo "       (先删一个、等 Running 再删下一个,保持服务不断)"
  fi
  finish
}

cmd_status() {
  STEP_TOTAL=1
  step "节点健康速览"
  echo "--- worker 容器:"
  docker ps -a --filter "name=${WORKER_NAME}" --format '  {{.Names}}  {{.Status}}  ({{.Image}})'
  echo "--- 本机 healthz:"
  curl -sf --max-time 3 "http://127.0.0.1:${WORKER_PORT}/healthz" && echo "  OK" || echo "  不通"
  echo "--- 镜像:"
  docker images --format '  {{.ID}}  {{.Repository}}:{{.Tag}}' | grep -E "gpustack|lightx2v" || true
  echo "--- NFS:"
  mountpoint -q /nfs-models && echo "  /nfs-models OK" || echo "  /nfs-models 未挂载"
  mountpoint -q /nfs-output && echo "  /nfs-output OK" || echo "  /nfs-output 未挂载"
  echo "--- GPU:"
  nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/  GPU /' || echo "  nvidia-smi 不可用"
  echo "--- 引擎实例容器:"
  docker ps --format '  {{.Names}}  {{.Status}}' | grep -vE "${WORKER_NAME}" | grep -E "run-0|pause" || echo "  (无)"
  finish
}

cmd_prepare_transfer() {
  parse_flags "$@"
  STEP_TOTAL=3
  mkdir -p "$TRANSFER_DIR"

  step "拉取 arm64 双镜像(x86 机器亦可)"
  docker pull --platform linux/arm64 "$GPUSTACK_IMAGE"
  docker pull --platform linux/arm64 "$ENGINE_IMAGE"

  step "save gpustack tar(必须 > 重定向,不能 -o,坑#5)"
  docker save "$GPUSTACK_IMAGE" > "${GPUSTACK_TAR}.tmp" &
  local pid=$!; watch_size $pid "${GPUSTACK_TAR}.tmp"; wait $pid
  mv "${GPUSTACK_TAR}.tmp" "$GPUSTACK_TAR"
  echo "    $(ls -lh "$GPUSTACK_TAR" | awk '{print $5, $9}')"

  step "save 引擎 tar(~29G,耐心)"
  docker save "$ENGINE_IMAGE" > "${ENGINE_TAR}.tmp" &
  pid=$!; watch_size $pid "${ENGINE_TAR}.tmp"; wait $pid
  mv "${ENGINE_TAR}.tmp" "$ENGINE_TAR"
  echo "    $(ls -lh "$ENGINE_TAR" | awk '{print $5, $9}')"
  echo "    提示:nvidia-repo/ 两件套如缺,在既有 GPU 节点执行:"
  echo "      mkdir -p ${NVIDIA_REPO_DIR} && cp /etc/apt/sources.list.d/nvidia-container-toolkit.list \\"
  echo "         /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ${NVIDIA_REPO_DIR}/"
  finish
}

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "请以 root 执行"
CMD="${1:-}"; shift || true
case "$CMD" in
  install)          cmd_install "$@" ;;
  upgrade-gpustack) cmd_upgrade_gpustack "$@" ;;
  upgrade-engine)   cmd_upgrade_engine "$@" ;;
  status)           cmd_status "$@" ;;
  prepare-transfer) cmd_prepare_transfer "$@" ;;
  *) usage ;;
esac
