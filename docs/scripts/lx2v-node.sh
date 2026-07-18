#!/usr/bin/env bash
# lx2v-node.sh — LightX2V/GPUStack GPU 节点一键安装与升级(fork 运维脚本)
#
# 用法(在 GPU 节点上以 root 执行):
#   ./lx2v-node.sh setup-base                        # 全新节点只配基础环境(docker/NFS/toolkit),不入集群
#   ./lx2v-node.sh install --token <GPUSTACK_TOKEN> [--worker-ip <IP>] [--offline] [--clean-residue] [--force]
#   ./lx2v-node.sh upgrade-gpustack [--offline]     # 换 gpustack:lx2v-dev 并原参数重启 worker
#   ./lx2v-node.sh upgrade-engine   [--engine lightx2v|indextts|acestep] [--offline]
#                                                    # 换引擎镜像(默认 lightx2v;实例需重建才生效)
#   ./lx2v-node.sh clean [--purge-data] [--kill-gpu-procs]   # 清理卸载残留(见下)
#   ./lx2v-node.sh status                            # 节点健康速览
#   ./lx2v-node.sh prepare-transfer                  # (238/有 ACR 外网的机器)拉四镜像(gpustack/lightx2v/indextts2/acestep)存 NFS tar
#
# 残留环境(装过 GPUStack 又卸载/被清理过的节点):
#   install 自带残留检测——异 token 的旧 worker 自动移除重建,同 token(同集群)需加 --force;
#   孤儿引擎实例容器默认只告警,加 --clean-residue 一并硬杀;旧 gpustack-data 卷默认**复用**。
#   要彻底重置节点:先 ./lx2v-node.sh clean --purge-data,再 install。
#   GPU 上的野进程(非 GPUStack 管理,坑#7/#17-1)任何命令都只告警不自动杀,
#   clean 加 --kill-gpu-procs 才会 kill -9。
#
# --offline:不走 ACR,直接从 NFS 的 _transfer/ tar docker load(全新节点/网络受限时用)。
# 进度可视:每步打印 [step i/N] 开始时间与耗时;长任务(load/pull/save)原生输出直通;
# 全程 tee 到 /var/log/lx2v-node-<日期>.log。任何失败都会打印原因分析与操作建议。
#
# 脚本无工作目录依赖,可放节点任意位置执行。分发到新节点的两种方式:
#   scp docs/scripts/lx2v-node.sh root@<新节点>:/root/          # 新节点还没挂 NFS 时
#   bash /nfs-models/_transfer/lx2v-node.sh …                   # 已挂 NFS 的节点直接跑
# (prepare-transfer 会自动把自身拷一份到 _transfer/,保持 NFS 上是最新版)
#
# 对应 runbook:docs/lightx2v-gpustack-部署实录.md §12.1/§17.7 与
# docs/lightx2v-20260706-发布部署验证全记录.md §4。
set -Eeuo pipefail

# ---------- 可调配置 ----------
REGISTRY="crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly"
GPUSTACK_IMAGE="${REGISTRY}/gpustack:lx2v-dev"
ENGINE_IMAGE="${REGISTRY}/lightx2v:arm64-a100-latest"
# IndexTTS-2 语音合成引擎(独立 CI 出包)。tag 必须与 gpustack 内置后端注册表
# (schemas/inference_backend.py 的 image_name)完全一致——worker 本地 load 后按名匹配。
INDEXTTS_IMAGE="${REGISTRY}/indextts2:arm64-a100-latest"
# ACE-Step-1.5 文生音乐引擎(独立 CI 出包,同 indextts 范式)。tag 同样须与
# gpustack 内置后端注册表(schemas/inference_backend.py 的 image_name)一致。
ACESTEP_IMAGE="${REGISTRY}/acestep:arm64-a100-latest"
SERVER_URL="${SERVER_URL:-http://10.0.0.238}"
NFS_SERVER="100.125.40.2"
NFS_MODELS_EXPORT="/share-LLM"
NFS_OUTPUT_EXPORT="/share-output"
TRANSFER_DIR="/nfs-models/_transfer"
GPUSTACK_TAR="${TRANSFER_DIR}/gpustack-lx2v-dev-arm64.tar"
ENGINE_TAR="${TRANSFER_DIR}/lightx2v-arm64-profiles.tar"
INDEXTTS_TAR="${TRANSFER_DIR}/indextts2-arm64-a100.tar"
ACESTEP_TAR="${TRANSFER_DIR}/acestep-arm64-a100.tar"
NVIDIA_REPO_DIR="${TRANSFER_DIR}/nvidia-repo"
WORKER_NAME="gpustack-worker"
WORKER_PORT=10150
# ------------------------------

LOG_FILE="/var/log/lx2v-node-$(date +%Y%m%d).log"
exec > >(tee -a "$LOG_FILE") 2>&1

STEP_NO=0
STEP_TOTAL=0
STEP_T0=0
CURRENT_STEP="(预检)"
step() {
  [ "$STEP_NO" -gt 0 ] && echo "    ... 上一步耗时 $((SECONDS - STEP_T0))s"
  STEP_NO=$((STEP_NO + 1))
  STEP_T0=$SECONDS
  CURRENT_STEP="$*"
  echo ""
  echo "==> [step ${STEP_NO}/${STEP_TOTAL}] $(date '+%H:%M:%S')  $*"
}
finish() {
  [ "$STEP_NO" -gt 0 ] && echo "    ... 上一步耗时 $((SECONDS - STEP_T0))s"
  echo ""
  echo "==> 完成:总耗时 $((SECONDS / 60))m$((SECONDS % 60))s  (日志: ${LOG_FILE})"
}

# die "错误信息" ["建议行1" "建议行2" ...] —— 带操作建议的失败退出
die() {
  local msg=$1; shift || true
  echo "" >&2
  echo "!! 失败(step: ${CURRENT_STEP}): ${msg}" >&2
  if [ $# -gt 0 ]; then
    echo "!! 建议:" >&2
    local line; for line in "$@"; do echo "     - ${line}" >&2; done
  fi
  echo "!! 完整日志: ${LOG_FILE}" >&2
  exit 1
}

# 未被 die 捕获的命令失败:打印通用坑速查(全记录 §4 / 部署实录 §17.4/§17.7)
on_error() {
  local rc=$? line=$1
  echo "" >&2
  echo "!! 命令失败(exit=${rc},脚本第 ${line} 行,step: ${CURRENT_STEP})" >&2
  echo "!! 常见坑速查:" >&2
  echo "     - apt 报 'Unable to locate package nvidia-container-toolkit':该包不在 Ubuntu 源," >&2
  echo "       需从既有节点拷 apt 源两件套到 ${NVIDIA_REPO_DIR}/(脚本会自动使用)" >&2
  echo "     - apt 一次装多包时一个失败会整体中止 → 其余包也没装上,须逐个重装" >&2
  echo "     - mount.nfs 失败:确认 nfs-common 已装、${NFS_SERVER} 可达、export 名正确" >&2
  echo "     - docker pull 超时/EOF:ACR 网络抖动 → 重试,或先在 238 跑 prepare-transfer 后用 --offline" >&2
  echo "     - docker load 报 unexpected EOF:NFS 上的 tar 是半截文件(save 未完成或曾用 -o)," >&2
  echo "       到 238 重新 prepare-transfer" >&2
  echo "     - worker 注册成功但 UI 不转 Ready:云安全组拦 TCP(ping 通、curl ${WORKER_PORT} 超时)" >&2
  echo "       → 把本节点换成既有 GPU 节点同款安全组" >&2
  echo "!! 完整日志: ${LOG_FILE};文档: docs/lightx2v-20260706-发布部署验证全记录.md §4" >&2
  exit "$rc"
}
trap 'on_error $LINENO' ERR

# 后台任务的文件大小进度条(docker save 无原生进度)
watch_size() { # watch_size <pid> <file>
  local pid=$1 file=$2
  while kill -0 "$pid" 2>/dev/null; do
    sleep 10
    [ -f "$file" ] && echo "    ... $(date '+%H:%M:%S') $(du -h "$file" 2>/dev/null | cut -f1) 已写入"
  done
}

# 镜像的 registry digest(RepoDigests,manifest-list 级,与本地拉的哪个平台无关;
# 排序拼接防多条目顺序抖动)。本地 build 的镜像无 RepoDigests → 返回空 → 永远重 save
image_digest() { # image_digest <image>
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" 2>/dev/null \
    | sort | tr '\n' ' ' || true
}

# save 去重:tar 旁存 <tar>.digest 标记。digest 未变且 tar 在 → 跳过(~10G/次的大头)。
# 完整性:tar 走 .tmp + mv 原子改名,tar 文件存在即写完;.digest 在 mv 之后才写,
# 标记在 = tar 完整。半截只会是 .tmp,进场先清(上次 Ctrl-C 的残留)。
save_tar_if_changed() { # save_tar_if_changed <image> <tar> [docker save 额外参数...]
  local image=$1 tar=$2; shift 2
  local digest; digest="$(image_digest "$image")"
  rm -f "${tar}.tmp"
  if [ -n "$digest" ] && [ -f "$tar" ] \
     && [ "$(cat "${tar}.digest" 2>/dev/null || true)" = "$digest" ]; then
    echo "    digest 未变($(du -h "$tar" | cut -f1) 已在 NFS),跳过 save: $tar"
    return 0
  fi
  docker save "$@" "$image" > "${tar}.tmp" &
  local pid=$!; watch_size $pid "${tar}.tmp"; wait $pid
  mv "${tar}.tmp" "$tar"
  if [ -n "$digest" ]; then echo "$digest" > "${tar}.digest"; fi
  echo "    $(du -h "$tar" | cut -f1)  ${tar}"
}

detect_worker_ip() {
  # 取 10.x 网段第一个地址;无匹配返回空(由调用方决定是否致命)
  hostname -I | tr ' ' '\n' | grep -E '^10\.' | head -1 || true
}

# 从旧 worker 容器的启动命令里取某个 flag 的值(如 --server-url / --worker-ip)
old_cmd_value() { # old_cmd_value <flag>
  docker inspect "$WORKER_NAME" --format '{{range .Config.Cmd}}{{println .}}{{end}}' 2>/dev/null \
    | awk -v k="$1" 'prev==k {print; exit} {prev=$0}' || true
}

# 解析 worker IP:显式 --worker-ip > 旧容器参数 > 自动探测。
# 继承依赖 docker inspect 旧容器,因此必须在旧容器被移除之前调用
resolve_worker_ip() {
  [ -n "$WORKER_IP" ] && return 0
  WORKER_IP="$(old_cmd_value --worker-ip)"
  [ -n "$WORKER_IP" ] && { echo "    worker-ip 继承自旧容器: ${WORKER_IP}"; return 0; }
  WORKER_IP="$(detect_worker_ip)"
  [ -n "$WORKER_IP" ] || die "无法确定 worker IP(本机无 10.x 地址,也无旧容器可继承)" \
    "请显式指定: --worker-ip <本机内网IP>(hostname -I 查看候选)"
}

parse_flags() {
  TOKEN="${GPUSTACK_TOKEN:-}"
  WORKER_IP=""
  ENGINE_SEL="lightx2v"
  OFFLINE=0
  CLEAN_RESIDUE=0
  PURGE_DATA=0
  KILL_GPU_PROCS=0
  FORCE=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --token) TOKEN="$2"; shift 2 ;;
      --worker-ip) WORKER_IP="$2"; shift 2 ;;
      --engine) ENGINE_SEL="$2"; shift 2 ;;
      --offline) OFFLINE=1; shift ;;
      --clean-residue) CLEAN_RESIDUE=1; shift ;;
      --purge-data) PURGE_DATA=1; shift ;;
      --kill-gpu-procs) KILL_GPU_PROCS=1; shift ;;
      --force) FORCE=1; shift ;;
      *) die "未知参数: $1" "用法见: $0(不带参数)" ;;
    esac
  done
  # 注意:此处不解析 worker IP——clean/upgrade-engine/prepare-transfer 不需要它,
  # 且非 10.x 网段机器上强行探测会失败。需要时由 resolve_worker_ip 按需解析。
}

# 孤儿引擎实例容器:优先按 gpustack-runtime 给容器打的 label 识别(稳定接口),
# 并保留命名正则兜底(老版本 runtime 无 label 时),两者取并集。
# 命名正则须覆盖 deployer 的全部产物:-run-N / -pause / -init-N / -unhealthy-restart
INSTANCE_NAME_RE='-run-[0-9]+$|-pause$|-init-[0-9]+$|-unhealthy-restart$'
list_instance_containers() {
  {
    docker ps -a --filter label=runtime.gpustack.ai/workload --format '{{.Names}}' 2>/dev/null || true
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E -- "$INSTANCE_NAME_RE" || true
  } | grep -vx "$WORKER_NAME" | sort -u || true
}

# GPU 上非 GPUStack 管理的进程(实验残留裸进程等,坑#7/#17-1:不清会 OOM)
list_gpu_procs() {
  nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null | sed 's/^/    PID /' || true
}

kill_instance_containers() {
  # 硬杀:kill + sleep + rm -f(引擎容器单发 docker rm 常卡住,见 memory/坑)
  local names; names="$(list_instance_containers)"
  [ -n "$names" ] || { echo "    无引擎实例容器"; return 0; }
  echo "$names" | xargs -r docker kill 2>/dev/null || true
  sleep 2
  echo "$names" | xargs -r docker rm -f 2>/dev/null || true
  echo "    已移除: $(echo "$names" | tr '\n' ' ')"
}

scan_residue() {
  echo "    -- 残留扫描 --"
  local found=0
  if docker inspect "$WORKER_NAME" > /dev/null 2>&1; then
    found=1
    echo "    [有] 旧 ${WORKER_NAME} 容器: $(docker ps -a --filter "name=${WORKER_NAME}" --format '{{.Status}} ({{.Image}})')"
    # token 比对判定场景:同 token=同集群(可能健康,误跑保护);
    # 异 token=接入新 server(如管理节点重装丢数据,老 token 已失效,重建即恢复路径)
    local old_token; old_token="$(current_worker_token)"
    if [ -n "$old_token" ] && [ "$old_token" = "$TOKEN" ]; then
      if [ "$FORCE" -eq 1 ]; then
        echo "         → 同 token(同集群),--force 已指定,将重建"
      else
        die "现有 worker 与本次 --token 相同(同集群,可能仍健康)" \
          "只想换 gpustack 镜像 → 用: $0 upgrade-gpustack" \
          "确认要重建(如容器损坏)→ 重跑 install 加 --force"
      fi
    else
      echo "         → token 与现有容器不同(典型场景:管理节点重装换了新集群 token,"
      echo "           老 token 已失效、worker 假 Up 真失联)。将移除重建;"
      echo "           旧集群的引擎实例容器已成孤儿,建议加 --clean-residue 一并清理"
    fi
  fi
  local inst; inst="$(list_instance_containers)"
  if [ -n "$inst" ]; then
    found=1
    echo "    [有] 引擎实例容器(可能是上一次部署的孤儿):"
    # shellcheck disable=SC2001  # 多行文本统一缩进,${var//} 参数展开不适用
    echo "$inst" | sed 's/^/         /'
    if [ "$CLEAN_RESIDUE" -eq 1 ]; then
      kill_instance_containers
    else
      echo "         → 未处理。若这台节点确定不再被原 server 管理,重跑加 --clean-residue"
      echo "           或先执行: $0 clean;若仍属同一集群,worker 重连后会自动接管/回收,可不动"
    fi
  fi
  if docker volume inspect gpustack-data > /dev/null 2>&1; then
    found=1
    echo "    [有] gpustack-data 卷(创建于 $(docker volume inspect gpustack-data --format '{{.CreatedAt}}'))"
    echo "         → 默认复用(同集群重接入的正确姿势,保留 worker 身份/缓存)。"
    echo "           换了 server/集群或状态可疑时: $0 clean --purge-data 后重装"
  fi
  local procs; procs="$(list_gpu_procs)"
  if [ -n "$procs" ]; then
    found=1
    echo "    [有] GPU 残留进程(GPUStack 看不见外部占用,不清会调度上去 OOM,坑#7):"
    echo "$procs"
    echo "         → 请人工确认后 kill -9 <PID>(或 $0 clean --kill-gpu-procs)"
  fi
  [ "$found" -eq 0 ] && echo "    干净,无残留"
  return 0
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

# 全新节点装机用:NFS tar 在就 load(内网快);不在则在线 pull——但 --offline
# 模式下 tar 缺失直接失败(offline 语义不允许出网,静默回退会在受限网络上挂死)
fetch_image_prefer_tar() { # fetch_image_prefer_tar <image> <tar>
  local image=$1 tar=$2
  if [ -f "$tar" ]; then
    echo "    从 NFS load: $tar ($(du -h "$tar" | cut -f1))"
    docker load -i "$tar"
  elif [ "$OFFLINE" -eq 1 ]; then
    die "--offline 模式但 NFS tar 不存在: $tar" \
      "先在 238(可出网机器)执行: $0 prepare-transfer" \
      "或去掉 --offline 允许在线拉取"
  else
    echo "    NFS tar 不存在,在线拉取 ${image} ..."
    docker pull "$image" || die "pull 失败且无 NFS tar: $tar" \
      "ACR 网络不通?在 238 跑 prepare-transfer 后用 --offline 重试"
  fi
  echo "    当前镜像: $(docker images --format '{{.ID}}  {{.Repository}}:{{.Tag}}' | grep -F "${image#*/}" | head -1)"
}

# 单个 NFS 挂载点独立配置(半配置节点上只补缺的那个,互不牵连)
ensure_mount() { # ensure_mount <export> <mountpoint>
  local exp=$1 mp=$2
  mkdir -p "$mp"
  if ! mountpoint -q "$mp"; then
    grep -qsE "[[:space:]]${mp}[[:space:]]" /etc/fstab || \
      echo "${NFS_SERVER}:${exp} ${mp} nfs rw,hard,nolock,noresvport,_netdev 0 0" >> /etc/fstab
    mount "$mp" 2>/dev/null || mount -a
  fi
  mountpoint -q "$mp" || die "${mp} 挂载失败" \
    "确认 nfs-common 已装、${NFS_SERVER} 网络可达、export ${exp} 名称正确" \
    "手测: mount -t nfs ${NFS_SERVER}:${exp} ${mp}"
}

# 兼容软链:目标不存在或已是软链 → -sfn 覆盖指向;是真目录/真文件 → 不能动
# (ln -sf 对真目录会把链建到目录里面,静默产生 /data/wuhanjisuan894 假象)
ensure_symlink() { # ensure_symlink <link_path>
  local link=$1 target=/nfs-models/wuhanjisuan894
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    die "${link} 已存在且是真实目录/文件,不能自动替换为软链" \
      "人工确认内容后: mv ${link} ${link}.bak && ln -sn ${target} ${link}" \
      "(引擎配置里的 ${link}/... 路径依赖这个软链指向 NFS)"
  fi
  ln -sfn "$target" "$link"
}

ensure_nfs() {
  ensure_mount "$NFS_MODELS_EXPORT" /nfs-models
  ensure_mount "$NFS_OUTPUT_EXPORT" /nfs-output
  ensure_symlink /nfs-data
  ensure_symlink /data
  ls /nfs-models/wuhanjisuan894/models/ > /dev/null || die "NFS 内容不可读" \
    "挂上了但目录结构不对?确认挂的是 ${NFS_MODELS_EXPORT} 而非其他 export"
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

# worker 的 env 列表(数组 WORKER_ENVS):install 用标准三件套;upgrade 从旧容器
# 复制全部 GPUSTACK_* 前缀 env(只按前缀取——Config.Env 里混着镜像自带的 PATH 等,
# 新旧镜像可能不同,不能盲抄)
declare -a WORKER_ENVS
build_default_envs() { # build_default_envs <token>
  WORKER_ENVS=(
    "GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME=${WORKER_NAME}"
    "GPUSTACK_TOKEN=$1"
    "GPUSTACK_EXTRA_MOUNTS=/nfs-models,/nfs-output,/nfs-data"
  )
}
collect_existing_envs() {
  mapfile -t WORKER_ENVS < <(docker inspect "$WORKER_NAME" \
    --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^GPUSTACK_' || true)
  printf '%s\n' "${WORKER_ENVS[@]:-}" | grep -q '^GPUSTACK_TOKEN=' \
    || die "旧容器 env 里读不到 GPUSTACK_TOKEN" \
         "容器可能损坏;改用 install --token <T> --force 重建"
}

# 旧容器缺标准 env 时按 install 默认值补齐(§5 时代/UI 生成的命令只有 TOKEN;
# 缺 EXTRA_MOUNTS 引擎容器会丢 /nfs-output 挂载,而注册/healthz 全正常——静默坏)
ensure_env_present() { # ensure_env_present <KEY> <标准值>
  printf '%s\n' "${WORKER_ENVS[@]}" | grep -q "^$1=" && return 0
  echo "    ⚠️ 旧容器缺 $1,按标准值补齐: $2"
  WORKER_ENVS+=("$1=$2")
}

# 确保 GPUSTACK_EXTRA_MOUNTS 里含某挂载路径。ensure_env_present 只在整条 env 缺失时补,
# 升级时旧容器已带 EXTRA_MOUNTS(旧值可能不含 /nfs-data)→ 必须在已有值上追加,
# 否则老节点升级永远拿不到 /nfs-data,s2v/vace 的 config(引用 /nfs-data/...)在容器里断。
ensure_extra_mount() { # ensure_extra_mount <host_path>
  local want=$1 i found=0 val
  for i in "${!WORKER_ENVS[@]}"; do
    case "${WORKER_ENVS[$i]}" in
      GPUSTACK_EXTRA_MOUNTS=*)
        found=1; val="${WORKER_ENVS[$i]#GPUSTACK_EXTRA_MOUNTS=}"
        case ",${val}," in
          *",${want},"*) : ;;  # 已含,不动
          *) WORKER_ENVS[i]="GPUSTACK_EXTRA_MOUNTS=${val},${want}"
             echo "    ⚠️ EXTRA_MOUNTS 追加 ${want}" ;;
        esac ;;
    esac
  done
  [ "$found" -eq 1 ] || WORKER_ENVS+=("GPUSTACK_EXTRA_MOUNTS=/nfs-models,/nfs-output,${want}")
}

run_worker() { # run_worker <worker_ip> <volume> <server_url>(env 取全局 WORKER_ENVS)
  local ip=$1 volume=$2 server_url=$3
  local env_flags=() e
  for e in "${WORKER_ENVS[@]}"; do env_flags+=(-e "$e"); done
  docker run -d --name "$WORKER_NAME" \
    "${env_flags[@]}" \
    --restart=unless-stopped --privileged --network=host \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --volume "${volume}:/var/lib/gpustack" \
    --volume /nfs-models:/nfs-models --volume /nfs-output:/nfs-output \
    --volume /nfs-data:/nfs-data \
    --runtime nvidia \
    "$GPUSTACK_IMAGE" \
    --server-url "$server_url" --worker-ip "$ip"
}

verify_worker() {
  local i logs
  for i in $(seq 1 30); do
    # 先整体捕获日志再 grep:pipefail 下 docker logs | grep -q 会因 grep 匹配后
    # 提前关管道,令 docker logs 收 SIGPIPE 非零退出,把已注册误判成未注册
    logs="$(docker logs "$WORKER_NAME" 2>&1 || true)"
    if grep -q "registered with worker_id" <<< "$logs"; then
      grep -E "Registering|registered" <<< "$logs" | tail -2
      break
    fi
    sleep 2
    [ "$i" -eq 30 ] && { tail -10 <<< "$logs"; die "worker 60s 内未注册成功"; }
  done
  # 注册日志先于 API server 绑定端口出现(worker.py 先 _register 后 _serve_apis),
  # 单次探测会竞速端口 bind,须短重试
  for i in $(seq 1 10); do
    curl -sf --max-time 3 "http://127.0.0.1:${WORKER_PORT}/healthz" > /dev/null && break
    sleep 2
    [ "$i" -eq 10 ] && die "本机 healthz 20s 内不通"
  done
  echo "    本机 healthz OK。⚠️ 若 UI 不转 Ready:检查云安全组(须与既有节点同组,"
  echo "    症状=server ping 通但 TCP ${WORKER_PORT} 超时,见全记录 §4.2 坑 C)"
}

# 基础环境三步(install 与 setup-base 共用):apt 基础包 / NFS 挂载 / nvidia-toolkit。
# step 计数走全局 STEP_NO,调用方把这 3 步计入自己的 STEP_TOTAL。
base_env_steps() {
  step "apt 基础包(逐个装,避免一包失败全中止)"
  apt-get update -q
  apt-get install -y -q docker.io
  apt-get install -y -q nfs-common

  step "挂载 NFS + 软链"
  ensure_nfs

  step "nvidia-container-toolkit(源自 NFS ${NVIDIA_REPO_DIR})"
  # 已配置则整步跳过:systemctl restart docker 会杀掉节点上所有运行中的容器
  # (含 scan_residue 承诺不动的引擎实例),只有首次配置才值得付这个代价
  if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
    echo "    nvidia runtime 已配置,跳过(不重启 docker,不影响运行中实例)"
  else
    if ! command -v nvidia-ctk > /dev/null; then
      [ -d "$NVIDIA_REPO_DIR" ] || die "缺 ${NVIDIA_REPO_DIR}(在既有节点: cp /etc/apt/sources.list.d/nvidia-container-toolkit.list 与 keyring 到该目录)"
      cp "${NVIDIA_REPO_DIR}/nvidia-container-toolkit-keyring.gpg" /usr/share/keyrings/
      cp "${NVIDIA_REPO_DIR}/nvidia-container-toolkit.list" /etc/apt/sources.list.d/
      apt-get update -q && apt-get install -y -q nvidia-container-toolkit
    fi
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
    docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia || die "docker nvidia runtime 未生效"
  fi
}

# setup-base:只配基础环境(docker/NFS/toolkit),不注册 GPUStack、不载业务镜像。
# 全新节点先跑它即可直接 docker run 做实验;之后随时可再 install 入集群。
cmd_setup_base() {
  STEP_TOTAL=4

  step "预检:GPU 驱动 / 架构"
  nvidia-smi -L || die "nvidia-smi 不可用" "先安装 GPU 驱动(A100 节点镜像通常自带,重装过系统的机器需补装)"
  [ "$(uname -m)" = "aarch64" ] || echo "    ⚠️ 非 arm64 机器,镜像 tar 是 arm64 的"

  base_env_steps
  finish
}

cmd_install() {
  parse_flags "$@"
  [ -n "$TOKEN" ] || die "install 需要 --token" \
    "在既有 worker 节点上取: docker inspect ${WORKER_NAME} --format '{{range .Config.Env}}{{println .}}{{end}}' | grep GPUSTACK_TOKEN" \
    "同一集群的注册令牌可复用于多台 worker"
  STEP_TOTAL=10

  step "预检:GPU 驱动 / 架构"
  nvidia-smi -L || die "nvidia-smi 不可用" "先安装 GPU 驱动(A100 节点镜像通常自带,重装过系统的机器需补装)"
  [ "$(uname -m)" = "aarch64" ] || echo "    ⚠️ 非 arm64 机器,镜像 tar 是 arm64 的"

  step "残留检测(装过 GPUStack 又卸载/清理过的节点)"
  scan_residue
  # 此时旧容器还在:worker IP 可从旧容器继承,且 IP 定不下来时秒级失败,
  # 不浪费后面 30 分钟;旧容器要到 step 9 起新容器前一刻才移除,
  # 中途任何一步失败节点上仍有原 worker
  resolve_worker_ip
  echo "    worker-ip=${WORKER_IP}"

  base_env_steps

  step "镜像:gpustack(NFS tar 优先,无则在线拉)"
  fetch_image_prefer_tar "$GPUSTACK_IMAGE" "$GPUSTACK_TAR"

  step "镜像:lightx2v 引擎(NFS tar 优先,无则在线拉)"
  fetch_image_prefer_tar "$ENGINE_IMAGE" "$ENGINE_TAR"

  step "镜像:indextts2 引擎(NFS tar 优先,无则在线拉)"
  # 全节点预载(同 lightx2v 思路):TTS 整卡单实例,调度器可落任意空闲卡,
  # 新节点装完即可被调度,免去"记得补跑 upgrade-engine --engine indextts"的人为坑
  fetch_image_prefer_tar "$INDEXTTS_IMAGE" "$INDEXTTS_TAR"

  step "镜像:acestep 引擎(NFS tar 优先,无则在线拉)"
  # 同 indextts:文生音乐整卡单实例,全节点预载即可被调度落任意空闲卡
  fetch_image_prefer_tar "$ACESTEP_IMAGE" "$ACESTEP_TAR"

  step "起 worker 并验证注册"
  docker rm -f "$WORKER_NAME" 2>/dev/null || true
  echo "    worker-ip=${WORKER_IP}  server-url=${SERVER_URL}"
  build_default_envs "$TOKEN"
  run_worker "$WORKER_IP" "gpustack-data" "$SERVER_URL"
  verify_worker
  finish
}

cmd_clean() {
  parse_flags "$@"
  STEP_TOTAL=4

  step "移除 worker 容器"
  if docker inspect "$WORKER_NAME" > /dev/null 2>&1; then
    docker rm -f "$WORKER_NAME" && echo "    已移除 ${WORKER_NAME}"
  else
    echo "    无 ${WORKER_NAME} 容器"
  fi

  step "硬杀引擎实例容器(kill + sleep + rm -f)"
  kill_instance_containers

  step "数据卷(--purge-data 才删)"
  if docker volume inspect gpustack-data > /dev/null 2>&1; then
    if [ "$PURGE_DATA" -eq 1 ]; then
      docker volume rm gpustack-data
      echo "    已删除 gpustack-data 卷(worker 身份/缓存清零,下次 install 全新注册)"
    else
      echo "    保留 gpustack-data 卷(创建于 $(docker volume inspect gpustack-data --format '{{.CreatedAt}}'))"
      echo "    ⚠️ 若本机曾以匿名卷运行,老数据可能在 64 位 hash 卷里:docker volume ls 逐个确认后再清"
    fi
  else
    echo "    无 gpustack-data 卷"
  fi

  step "GPU 残留进程(--kill-gpu-procs 才杀)"
  local procs; procs="$(list_gpu_procs)"
  if [ -z "$procs" ]; then
    echo "    GPU 干净"
  elif [ "$KILL_GPU_PROCS" -eq 1 ]; then
    nvidia-smi --query-compute-apps=pid --format=csv,noheader | xargs -r kill -9 || true
    sleep 2
    echo "    已 kill -9,当前:"; list_gpu_procs; echo "    (无输出=已清空)"
  else
    echo "$procs"
    echo "    → 未杀(需 --kill-gpu-procs 或人工 kill -9);GPUStack 看不见外部占用,不清会 OOM(坑#7)"
  fi
  echo ""
  echo "    提示:镜像默认全保留(重装可增量复用);确要清理: docker image prune"
  finish
}

cmd_upgrade_gpustack() {
  parse_flags "$@"
  STEP_TOTAL=4
  docker inspect "$WORKER_NAME" > /dev/null 2>&1 || die "本机没有 ${WORKER_NAME} 容器(全新节点请用 install)"

  step "读取现有 worker 配置(GPUSTACK_* env / 卷 / server-url / IP 全部原样保留)"
  local volume old_server_url
  collect_existing_envs
  ensure_env_present GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME "$WORKER_NAME"
  ensure_env_present GPUSTACK_EXTRA_MOUNTS "/nfs-models,/nfs-output,/nfs-data"
  ensure_extra_mount /nfs-data   # 旧容器已带 EXTRA_MOUNTS 时补挂 /nfs-data(s2v/vace 依赖)
  ensure_symlink /nfs-data       # 宿主软链兜底(个别节点当年 install 未建上)
  volume="$(current_worker_volume)"
  [ -n "$volume" ] || die "读不到数据卷名" \
    "匿名卷也会有 64 位卷名;完全为空说明容器没挂 /var/lib/gpustack,重建会丢状态,停止" \
    "人工核对: docker inspect ${WORKER_NAME} --format '{{json .Mounts}}'"
  old_server_url="$(old_cmd_value --server-url)"
  [ -n "$old_server_url" ] || old_server_url="$SERVER_URL"
  resolve_worker_ip   # 显式 --worker-ip > 旧容器参数 > 自动探测
  echo "    volume=${volume}  worker-ip=${WORKER_IP}  server-url=${old_server_url}"
  echo "    继承 env: $(printf '%s\n' "${WORKER_ENVS[@]}" | cut -d= -f1 | tr '\n' ' ')"

  step "拉取/加载新 gpustack 镜像"
  fetch_image "$GPUSTACK_IMAGE" "$GPUSTACK_TAR"

  step "重建 worker 容器"
  docker stop "$WORKER_NAME" && docker rm "$WORKER_NAME"
  run_worker "$WORKER_IP" "$volume" "$old_server_url"

  step "验证注册"
  verify_worker
  finish
}

cmd_upgrade_engine() {
  parse_flags "$@"
  # --engine 选择要换的引擎镜像;默认 lightx2v,与历史行为一致
  local img tar
  case "$ENGINE_SEL" in
    lightx2v) img="$ENGINE_IMAGE";   tar="$ENGINE_TAR" ;;
    indextts) img="$INDEXTTS_IMAGE"; tar="$INDEXTTS_TAR" ;;
    acestep)  img="$ACESTEP_IMAGE";  tar="$ACESTEP_TAR" ;;
    *) die "未知引擎: $ENGINE_SEL" "--engine 只支持 lightx2v | indextts | acestep" ;;
  esac
  STEP_TOTAL=2
  step "当前引擎镜像(${ENGINE_SEL})"
  local old_id
  old_id="$(docker images --format '{{.ID}}' "$img" | head -1 || true)"
  echo "    old=${old_id:-<无>}"

  step "拉取/加载新引擎镜像"
  fetch_image "$img" "$tar"
  local new_id
  new_id="$(docker images --format '{{.ID}}' "$img" | head -1)"
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
  docker images --format '  {{.ID}}  {{.Repository}}:{{.Tag}}' | grep -E "gpustack|lightx2v|indextts|acestep" || true
  echo "--- NFS:"
  mountpoint -q /nfs-models && echo "  /nfs-models OK" || echo "  /nfs-models 未挂载"
  mountpoint -q /nfs-output && echo "  /nfs-output OK" || echo "  /nfs-output 未挂载"
  echo "--- GPU:"
  nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/  GPU /' || echo "  nvidia-smi 不可用"
  echo "--- 引擎实例容器:"
  local inst n
  inst="$(list_instance_containers)"
  if [ -n "$inst" ]; then
    while IFS= read -r n; do
      docker ps -a --filter "name=^${n}$" --format '  {{.Names}}  {{.Status}}'
    done <<< "$inst"
  else
    echo "  (无)"
  fi
  finish
}

cmd_prepare_transfer() {
  parse_flags "$@"
  STEP_TOTAL=5
  # tar 必须落在共享 NFS 上;未挂载时 mkdir -p 会静默建本地目录,
  # 大 tar(引擎/indextts 各 ~10G)写进根盘且其他节点拿不到
  mountpoint -q /nfs-models || die "/nfs-models 未挂载,拒绝把 tar 写到本地盘" \
    "先挂 NFS(fstab 两行 + mount -a,见 install 的 ② 或全记录 §4.2)再重试"
  mkdir -p "$TRANSFER_DIR"

  step "拉取 arm64 三镜像(x86 机器亦可)"
  docker pull --platform linux/arm64 "$GPUSTACK_IMAGE"
  docker pull --platform linux/arm64 "$ENGINE_IMAGE"
  docker pull --platform linux/arm64 "$INDEXTTS_IMAGE"
  docker pull --platform linux/arm64 "$ACESTEP_IMAGE"

  step "save gpustack tar(digest 未变则跳过;必须 > 重定向,不能 -o,坑#5)"
  # --platform:gpustack 镜像是多架构 manifest;containerd 镜像存储下,不带 --platform
  # 的 docker save 会尝试导出整个 manifest list(含未拉的 amd64)→ "content digest
  # not found"。指定 arm64 只导该平台,免去"先拉 amd64 占本地"的前置步骤。
  save_tar_if_changed "$GPUSTACK_IMAGE" "$GPUSTACK_TAR" --platform linux/arm64
  if [ "$(uname -m)" = "x86_64" ]; then
    # 238 的 server 容器与本 tag 同名:上面 --platform arm64 的 pull 已把本地 tag
    # 指向 arm64 镜像,不恢复的话之后跳过 pull 直接 docker run 会 exec format error
    echo "    恢复本地 amd64 tag(server 与本 tag 同名)..."
    docker pull --platform linux/amd64 "$GPUSTACK_IMAGE"
  fi

  step "save 引擎 tar(~10G,digest 未变则跳过)"
  save_tar_if_changed "$ENGINE_IMAGE" "$ENGINE_TAR"

  step "save indextts2 tar(~10G,digest 未变则跳过)"
  save_tar_if_changed "$INDEXTTS_IMAGE" "$INDEXTTS_TAR"

  step "save acestep tar(~8G,digest 未变则跳过)"
  save_tar_if_changed "$ACESTEP_IMAGE" "$ACESTEP_TAR"
  cp -f "$0" "${TRANSFER_DIR}/lx2v-node.sh" && chmod +x "${TRANSFER_DIR}/lx2v-node.sh"
  echo "    脚本自身已同步到 ${TRANSFER_DIR}/lx2v-node.sh(已挂 NFS 的节点可直接执行)"
  echo "    提示:nvidia-repo/ 两件套如缺,在既有 GPU 节点执行:"
  echo "      mkdir -p ${NVIDIA_REPO_DIR} && cp /etc/apt/sources.list.d/nvidia-container-toolkit.list \\"
  echo "         /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ${NVIDIA_REPO_DIR}/"
  finish
}

usage() {
  # 打印文件头整段注释(到第一个非注释行为止),不硬编码行号避免头注释增删后截断
  awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "请以 root 执行"
CMD="${1:-}"; shift || true
case "$CMD" in
  install)          cmd_install "$@" ;;
  setup-base)       cmd_setup_base "$@" ;;
  upgrade-gpustack) cmd_upgrade_gpustack "$@" ;;
  upgrade-engine)   cmd_upgrade_engine "$@" ;;
  clean)            cmd_clean "$@" ;;
  status)           cmd_status "$@" ;;
  prepare-transfer) cmd_prepare_transfer "$@" ;;
  *) usage ;;
esac
