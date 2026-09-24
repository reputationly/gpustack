#!/usr/bin/env bash
# lx2v-node.sh — LightX2V/GPUStack GPU 节点一键安装与升级(fork 运维脚本)
#
# 用法(在 GPU 节点上以 root 执行):
#   ./lx2v-node.sh setup-base                        # 全新节点只配基础环境(docker/NFS/toolkit),不入集群
#   ./lx2v-node.sh install --token <GPUSTACK_TOKEN> [--worker-ip <IP>] [--offline] [--clean-residue] [--force]
#   ./lx2v-node.sh upgrade-gpustack [--offline]     # 换 gpustack:lx2v-dev 并原参数重启 worker
#   ./lx2v-node.sh mount-nfs                         # 只补 NFS 挂载(含 prod 模型 /root/Models,只读),不碰容器
#   ./lx2v-node.sh rebuild-worker                    # 不换镜像,原参数重建 worker(用于新增挂载)
#   ./lx2v-node.sh upgrade-engine   [--engine lightx2v|acestep|vllm-omni|breeze|yue2|vllm-backport] [--offline]
#       vllm-backport(DeepSeek-V4-Flash 引擎)的 tag 必须与 GPUStack 模型配置一致,用
#       VLLM_BACKPORT_IMAGE=<完整镜像:tag> 显式指定;默认 latest 拉了也用不上
#                                                    # 换引擎镜像(默认 lightx2v;实例需重建才生效)
#                                                    # indextts/bernini 已下线,不再默认分发,但 --engine 仍可手动指定
#   ./lx2v-node.sh clean [--purge-data] [--kill-gpu-procs]   # 清理卸载残留(见下)
#   ./lx2v-node.sh status                            # 节点健康速览
#   ./lx2v-node.sh prepare-transfer                  # (238/有 ACR 外网的机器)拉六镜像(gpustack/lightx2v/acestep/vllm-omni/breeze-tts/yue2)存 NFS tar
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
# [已下线 2026-08-25] IndexTTS-2 语音合成引擎。其能力已由 vllm-omni 承接
# (indextts-2 模型现在跑在 vLLMOmni 后端上),不再默认预载/分发。
# 变量与 upgrade-engine 分支保留,需要时:./lx2v-node.sh upgrade-engine --engine indextts
INDEXTTS_IMAGE="${REGISTRY}/indextts2:arm64-a100-latest"
# ACE-Step-1.5 文生音乐引擎(独立 CI 出包,同 indextts 范式)。tag 同样须与
# gpustack 内置后端注册表(schemas/inference_backend.py 的 image_name)一致。
ACESTEP_IMAGE="${REGISTRY}/acestep:arm64-a100-latest"
# vLLM-Omni 全模型通用引擎(一镜像跑全部语音/音频模型;独立 CI 出包)。tag 须与
# gpustack 内置后端注册表(schemas/inference_backend.py 的 image_name)一致——
# 后端注册在 P3;注册前本镜像仅供分发/手测,GPUStack 尚不会调度它。
VLLM_OMNI_IMAGE="${REGISTRY}/vllm-omni:arm64-a100-latest"
# [已下线 2026-08-25] Bernini 原生视频生成/编辑引擎。不再默认预载/分发。
# 注意它仍停留在 cu128 base:Bernini/pyproject.toml 声明 requires-python
# ">=3.11,<3.12",与 cu130 base 的 Python 3.12 冲突,无法直接复用新 base
# (详见 gpustack 仓 docs/cu130-py312-upgrade-2026-08-24.md §2.2)。
# 变量与 upgrade-engine 分支保留,需要时:./lx2v-node.sh upgrade-engine --engine bernini
BERNINI_IMAGE="${REGISTRY}/bernini:arm64-a100-latest"
# Breeze TTS 2 音色设计引擎(独立 CI 出包,同 acestep 范式)。接替 MOSS-VoiceGen:
# 纯文字描述造声线,无参考音频。已在 GPUStack 注册为内置后端 BreezeTTS,会被调度。
BREEZE_IMAGE="${REGISTRY}/breeze-tts:arm64-a100-latest"
# YuE2 文生音乐 / 翻唱引擎(独立 CI 出包,reputationly/YuE)。已在 GPUStack 注册为内置后端
# YuE2,会被调度。2026-09-24 起(YuE2-Turbo,AR 跑 vLLM)叠在 vllm-omni 基座上 —— 与 vllm-omni
# 镜像的 52 层逐层相同,自身只多 4 层 ~160MB。所以装过 vllm-omni 的节点在线拉它只下 app 层
# (gpu41 实测 11s),反而是 NFS tar(docker save 整镜像,≈vllm-omni tar + 160MB)要把基座
# 再读一遍 —— 见 install 里它的预载方式。
YUE2_IMAGE="${REGISTRY}/yue2:arm64-a100-latest"
# vLLM-backport:DeepSeek-V4-Flash(含 1M 上下文版)跑的引擎,sm80 专用构建。
#
# 它与本脚本里其他引擎的**归属不同**,别按同一套心智模型理解:
#   · lightx2v / acestep / breeze 等是自带镜像的第一类后端,镜像声明在
#     gpustack/schemas/inference_backend.py 的 version_configs.image_name 里;
#   · vLLM 用的是 GPUStack **官方内置后端**(`InferenceBackend(VLLM, is_built_in=True)`,
#     没有 version_configs),我们的 backport 镜像是在**模型自己的 YAML 里配 image_name**
#     挂上去的 —— 走 base._resolve_image 的第 1 优先级(模型 image_name > 后端
#     version_configs > gpustack-runner 自动推导)。
# 所以本脚本对它**没有权威的版本来源**:改版本是改 GPUStack 里模型的 YAML,
# 本脚本只负责把那个 tag 的镜像预分发到节点,省掉起实例时现场拉 11G。
#
# 由此产生一条硬约束:**tag 必须与模型 YAML 里的 image_name 逐字一致**。
# 其余引擎用 `:arm64-a100-latest` 这种可变 tag,拉 latest 就等于拉到实例会用的那个;
# 而 backport 的现网模型 YAML 钉的是带时间戳的不可变 tag(如
# arm64-sm80-20260915-0047-85d0e70c)。此时拉 `:arm64-sm80-latest` **毫无用处** ——
# 它只会在本地多出一个 latest 标签,gpustack 起实例时按 YAML 里的时间戳 tag 去找,
# 本地没有就现场拉;更糟的是本地若有个旧的同名 tag,按 IfNotPresent 会直接用旧的
# (2026-09-17 全队实测:20 台节点的本地 arm64-sm80-latest 都停在旧构建上)。
# 所以分发时用 VLLM_BACKPORT_IMAGE 显式传 YAML 里那个 tag:
#   VLLM_BACKPORT_IMAGE=.../vllm-backport:arm64-sm80-20260915-0047-85d0e70c \
#     ./lx2v-node.sh upgrade-engine --engine vllm-backport
# 默认值给 latest 只是为了不带参数时也能跑通,生产分发请显式指定。
VLLM_BACKPORT_IMAGE="${VLLM_BACKPORT_IMAGE:-${REGISTRY}/vllm-backport:arm64-sm80-latest}"
SERVER_URL="${SERVER_URL:-http://10.0.0.238}"
NFS_SERVER="100.125.40.2"
NFS_MODELS_EXPORT="/share-LLM"
NFS_OUTPUT_EXPORT="/share-output"
# prod(newapi)集群的模型 export,挂到与 prod 相同的 /root/Models:prod 的模型配置
# (模型路径、--chat-template 等)在 dev 上可原样使用。只读,防止 dev 上的实验误改线上模型。
NFS_PROD_MODELS_EXPORT="/MaaS_Models"
PROD_MODELS_MP="/root/Models"
PROD_MODELS_OPTS="ro,hard,nolock,noresvport,_netdev"
TRANSFER_DIR="/nfs-models/_transfer"
GPUSTACK_TAR="${TRANSFER_DIR}/gpustack-lx2v-dev-arm64.tar"
ENGINE_TAR="${TRANSFER_DIR}/lightx2v-arm64-profiles.tar"
INDEXTTS_TAR="${TRANSFER_DIR}/indextts2-arm64-a100.tar"
ACESTEP_TAR="${TRANSFER_DIR}/acestep-arm64-a100.tar"
VLLM_OMNI_TAR="${TRANSFER_DIR}/vllm-omni-arm64-a100.tar"
BERNINI_TAR="${TRANSFER_DIR}/bernini-arm64-a100.tar"
BREEZE_TAR="${TRANSFER_DIR}/breeze-tts-arm64-a100.tar"
YUE2_TAR="${TRANSFER_DIR}/yue2-arm64-a100.tar"
# tar 名按 tag 派生,不用固定名:backport 是多版本并存的(现网钉时间戳 tag),
# 固定名的 tar 会让"NFS 上这个 tar 是哪一版"无从判断,回退时必然踩错版本。
VLLM_BACKPORT_TAR="${TRANSFER_DIR}/vllm-backport-${VLLM_BACKPORT_IMAGE##*:}.tar"
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
# 注意:依赖本地已有该镜像(inspect 读的是本地),所以只能用在 pull 之后。
image_digest() { # image_digest <image>
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" 2>/dev/null \
    | sort | tr '\n' ' ' || true
}

# 远端镜像指纹 —— **不拉取**即可获知"远端变没变",用于在 pull 之前就短路掉。
#
# 为什么不用 image_digest:它 inspect 本地镜像,必须先 pull,省不掉那一步。
# 为什么不取 `docker manifest inspect --verbose` 里的 Descriptor.digest:
#   多架构镜像(gpustack:lx2v-dev 就是)的 --verbose 会把每个平台的 manifest
#   和每一层的 digest 全列出来(实测 120 个 digest),第一个是**某个平台**的
#   manifest digest(如 536c9169…),而 RepoDigests 记的是**顶层 manifest list**
#   的 digest(166ca063…),两者对不上 —— 直接 grep 第一个会静默拿到错的值。
#   要精确复刻顶层 digest 得读 registry API 的 Docker-Content-Digest 响应头,
#   那需要先换 Bearer token,而节点上 buildx/crane/skopeo/jq 一个都没有。
# 取整体输出的 sha256 作为自洽指纹:天然覆盖所有平台与层,实测连查 3 次稳定。
#   它不等于官方 digest(docker 会 pretty-print),但我们只需要"变没变"这一位信息。
#   docker 版本升级若改变输出格式,指纹会变 → 多做一次 pull+save,无害。
# 查不到远端(网络不通 / 鉴权超时)必须返回**空**,不能返回空串的 sha256。
# 坑#9:原写法 `docker manifest inspect ... | sha256sum` 里管道左边失败时右边照跑,
#   得到 e3b0c442…(空输入的 sha256)—— 非空,于是被当成合法指纹。ACR 抖动时
#   两次失败会拿到同一个 e3b0c442,marker 一旦被它写过,以后每次断网都判"远端未变"
#   而跳过 pull+save,静默把旧 tar 当新的发下去。现在:失败/空输出 → 返回空。
remote_fingerprint() { # remote_fingerprint <image>
  local out
  out="$(docker manifest inspect --verbose "$1" 2>/dev/null)" || return 0
  [ -n "$out" ] || return 0
  printf '%s' "$out" | sha256sum | cut -d' ' -f1
}

# ACR(cn-shanghai personal)时不时 TLS handshake timeout,单发 pull 一挂就整段中止。
# 退避重试 3 次;仍失败才交给调用方(die / soft 告警)。
docker_pull_retry() { # docker_pull_retry <docker pull 的全部参数...>
  local i
  for i in 1 2 3; do
    if docker pull "$@"; then return 0; fi
    [ "$i" -lt 3 ] && { echo "    pull 失败(第 ${i}/3 次,ACR 抖动?),${i}0s 后重试 ..."; sleep "${i}0"; }
  done
  return 1
}

# 同步单个镜像到 NFS tar:远端未变则连 pull 都跳过。
# 两道去重:① 远端指纹(省 pull)② RepoDigests(省 save,~10G 的大头)。
# 指纹在 save 成功之后才落盘,中途失败下次会重来,不会留下"标记在但 tar 是旧的"。
sync_image_to_nfs() { # sync_image_to_nfs <image> <tar> [docker save 额外参数...]
  local image=$1 tar=$2; shift 2
  local marker="${tar}.remote"
  local fp; fp="$(remote_fingerprint "$image")"
  if [ -n "$fp" ] && [ -f "$tar" ] \
     && [ "$(cat "$marker" 2>/dev/null || true)" = "$fp" ]; then
    echo "    远端未变(指纹 ${fp:0:12},$(du -h "$tar" | cut -f1) 已在 NFS),跳过 pull+save: $tar"
    return 0
  fi
  [ -n "$fp" ] || echo "    ⚠️ 查不到远端指纹(网络/鉴权?),无法判断是否变化 → 照常 pull"
  docker_pull_retry --platform linux/arm64 "$image" \
    || die "pull 失败(已重试 3 次): $image" \
         "ACR 抖动:稍后重跑 prepare-transfer(已同步的 tar 会跳过,不重来)" \
         "NFS 上的 ${tar##*/} 仍是上一次的旧版本,别当新的发下去"
  save_tar_if_changed "$image" "$tar" "$@"
  # 坑#10:这里必须用 if,不能写 `[ -n "$fp" ] && echo ...` —— 它是函数最后一条命令,
  #   条件为假时函数返回 1,set -e + ERR trap 会把整段 prepare-transfer 判死(明明
  #   pull 和 save 都成功了)。之前 fp 恒非空(见坑#9)才一直没暴露。
  # 拿不到指纹就删掉旧 marker:留着等于宣称一个与当前 tar 未必对应的远端状态,
  #   下次顶多多做一次 pull+save,无害。
  if [ -n "$fp" ]; then echo "$fp" > "$marker"; else rm -f "$marker"; fi
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
    # 有旧镜像时 pull 只拉增量层;失败才回退 NFS tar。
    #
    # 坑#11:这里原先是裸 `docker pull`(不是 docker_pull_retry),ACR 一抖 **第一次失败
    #   就回退**,而回退前又不校验 tar 新旧 —— 于是 upgrade-gpustack/upgrade-engine 会
    #   报「完成」却装上 NFS 上的旧镜像。这正是 sync_image_to_nfs 那边花大力气防的静默
    #   降级(坑#9),只是发生在节点这一端,而且更隐蔽:出 tar 那侧有指纹 marker 兜底,
    #   装机这侧什么都不查。现在:① 先按 docker_pull_retry 退避重试 3 次;② 回退前拿
    #   远端指纹和 <tar>.remote 比对,**能证明 tar 是旧的就直接失败**,不硬装。
    if ! docker_pull_retry "$image"; then
      [ -f "$tar" ] || die "pull 失败(已重试 3 次)且无 NFS tar 可用: $tar" \
        "ACR 网络不通?在 238 跑 prepare-transfer 出 tar 后用 --offline 重试"
      local fp marker
      marker="${tar}.remote"
      fp="$(remote_fingerprint "$image")"
      if [ -n "$fp" ] && [ -n "$(cat "$marker" 2>/dev/null || true)" ] \
         && [ "$(cat "$marker" 2>/dev/null)" != "$fp" ]; then
        die "pull 失败,且 NFS tar 已确认是旧版本,拒绝静默降级: $tar" \
          "远端指纹 ${fp:0:12} ≠ tar 指纹 $(cut -c1-12 "$marker" 2>/dev/null)" \
          "在 238 跑 prepare-transfer 把 tar 更新到当前远端,再重跑本命令" \
          "确实要装这个旧版本 → 显式用 --offline(语义上就是「用 NFS 上的那一版」)"
      fi
      if [ -z "$fp" ]; then
        # pull 刚失败,查指纹大概率也查不到 —— 无法证明 tar 新旧。不阻塞恢复,
        # 但必须让日志里这件事无法被忽略:fleet 汇总只看退出码,OK 会掩盖一切。
        echo "    ⚠️⚠️ 无法校验 NFS tar 是否为当前版本(远端指纹查不到,网络不通?)"
        echo "    ⚠️⚠️ 即将 load 的 tar 修改时间: $(date -r "$tar" '+%F %T' 2>/dev/null || echo 未知)"
        echo "    ⚠️⚠️ 若它早于最近一次构建,本次升级实际是**降级**,请事后核对镜像 ID"
      else
        echo "    NFS tar 指纹与远端一致(${fp:0:12}),回退安全"
      fi
      echo "    pull 失败,回退 NFS tar ..."
      docker load -i "$tar"
    fi
  fi
  echo "    当前镜像: $(docker images --format '{{.ID}}  {{.Repository}}:{{.Tag}}' | grep -F "${image#*/}" | head -1)"
}

# 升级成功后回收悬空镜像。放在验证之后:失败时旧镜像还在,能回退。
#
# 为什么必须做而不是只提示:docker 这套用的是 containerd snapshotter
# (`docker info` → Storage Driver: overlayfs / driver-type io.containerd.snapshotter.v1),
# 镜像落在 /var/lib/containerd 而非 /var/lib/docker,且**压缩 blob 与解开的层各存一份**
# (content.v1.content 107G + snapshotter.v1.overlayfs 295G,实测于 0043)。一个
# backport 版本约占 29~31G。2026-09-17 全队实测:每台已攒下 38~46 个悬空镜像,
# 55 台清出 2.9TB(平均 54.6G/台),0043 当时只剩 486G。靠 runbook 里写句「确要清理」
# 是不管用的 —— 这次就是攒到接近告警线才被发现。
prune_dangling() {
  local before after
  before="$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -d ' G')"
  # 只删悬空镜像:有 tag 的、以及被任何容器(含已停止)引用的都不会被碰,
  # 所以不会动 gpustack-worker 和正在跑的引擎实例。
  docker image prune -f || echo "    ⚠️ prune 失败(不影响升级结果)"
  after="$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -d ' G')"
  if [ -n "$before" ] && [ -n "$after" ]; then
    echo "    根分区剩余 ${before}G → ${after}G(释放 $((after - before))G)"
  fi
}

# 全新节点装机用:NFS tar 在就 load(内网快);不在则在线 pull——但 --offline
# 模式下 tar 缺失直接失败(offline 语义不允许出网,静默回退会在受限网络上挂死)
fetch_image_prefer_tar() { # fetch_image_prefer_tar <image> <tar> [soft]
  # soft=第三参数 "soft":镜像缺失(无 tar 且拉不到)时只告警不 die。用于尚未在
  # GPUStack 注册、不会被调度的引擎(如 vllm-omni),缺它不该阻塞整个 install。
  local image=$1 tar=$2 soft=${3:-}
  if [ -f "$tar" ]; then
    echo "    从 NFS load: $tar ($(du -h "$tar" | cut -f1))"
    docker load -i "$tar"
  elif [ "$OFFLINE" -eq 1 ]; then
    if [ "$soft" = "soft" ]; then
      echo "    ⚠️ (soft) --offline 且无 tar: $tar — 跳过(未注册引擎,不阻塞 install;需要时 upgrade-engine)"
      return 0
    fi
    die "--offline 模式但 NFS tar 不存在: $tar" \
      "先在 238(可出网机器)执行: $0 prepare-transfer" \
      "或去掉 --offline 允许在线拉取"
  else
    echo "    NFS tar 不存在,在线拉取 ${image} ..."
    if [ "$soft" = "soft" ]; then
      docker pull "$image" || echo "    ⚠️ (soft) pull 失败且无 tar: $image — 跳过(未注册引擎,不阻塞 install)"
    else
      docker pull "$image" || die "pull 失败且无 NFS tar: $tar" \
        "ACR 网络不通?在 238 跑 prepare-transfer 后用 --offline 重试"
    fi
  fi
  echo "    当前镜像: $(docker images --format '{{.ID}}  {{.Repository}}:{{.Tag}}' | grep -F "${image#*/}" | head -1)"
}

# 单个 NFS 挂载点独立配置(半配置节点上只补缺的那个,互不牵连)
ensure_mount() { # ensure_mount <export> <mountpoint> [mount options]
  local exp=$1 mp=$2 opts=${3:-rw,hard,nolock,noresvport,_netdev}
  # 非空的本地目录会被挂载静默遮住,里面的东西看起来像"丢了"
  if [ -d "$mp" ] && ! mountpoint -q "$mp" && [ -n "$(ls -A "$mp" 2>/dev/null)" ]; then
    die "${mp} 是非空的本地目录,挂载会把它遮住" \
      "人工确认内容后: mv ${mp} ${mp}.bak,再重跑"
  fi
  mkdir -p "$mp"
  if ! mountpoint -q "$mp"; then
    grep -qsE "[[:space:]]${mp}[[:space:]]" /etc/fstab || \
      echo "${NFS_SERVER}:${exp} ${mp} nfs ${opts} 0 0" >> /etc/fstab
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
  ensure_mount "$NFS_PROD_MODELS_EXPORT" "$PROD_MODELS_MP" "$PROD_MODELS_OPTS"
  ensure_symlink /nfs-data
  ensure_symlink /data
  ls /nfs-models/wuhanjisuan894/models/ > /dev/null || die "NFS 内容不可读" \
    "挂上了但目录结构不对?确认挂的是 ${NFS_MODELS_EXPORT} 而非其他 export"
  ls "$PROD_MODELS_MP" > /dev/null || die "${PROD_MODELS_MP} 不可读" \
    "确认挂的是 ${NFS_PROD_MODELS_EXPORT}"
  echo "    NFS OK(/nfs-models + /nfs-output + ${PROD_MODELS_MP}(只读) + 软链)"
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
    "GPUSTACK_EXTRA_MOUNTS=/nfs-models,/nfs-output,/nfs-data,${PROD_MODELS_MP}"
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

# upgrade-gpustack 与 rebuild-worker 共用:读旧 worker 的 env / 卷 / server-url / IP,
# 并补齐标准挂载。结果放在全局 WORKER_ENVS / WORKER_VOLUME / WORKER_SERVER_URL / WORKER_IP
collect_worker_config() {
  collect_existing_envs
  ensure_env_present GPUSTACK_RUNTIME_DEPLOY_MIRRORED_NAME "$WORKER_NAME"
  ensure_env_present GPUSTACK_EXTRA_MOUNTS "/nfs-models,/nfs-output,/nfs-data,${PROD_MODELS_MP}"
  ensure_extra_mount /nfs-data   # 旧容器已带 EXTRA_MOUNTS 时补挂 /nfs-data(s2v/vace 依赖)
  ensure_extra_mount "$PROD_MODELS_MP"
  ensure_symlink /nfs-data       # 宿主软链兜底(个别节点当年 install 未建上)
  WORKER_VOLUME="$(current_worker_volume)"
  [ -n "$WORKER_VOLUME" ] || die "读不到数据卷名" \
    "匿名卷也会有 64 位卷名;完全为空说明容器没挂 /var/lib/gpustack,重建会丢状态,停止" \
    "人工核对: docker inspect ${WORKER_NAME} --format '{{json .Mounts}}'"
  WORKER_SERVER_URL="$(old_cmd_value --server-url)"
  [ -n "$WORKER_SERVER_URL" ] || WORKER_SERVER_URL="$SERVER_URL"
  resolve_worker_ip   # 显式 --worker-ip > 旧容器参数 > 自动探测
  echo "    volume=${WORKER_VOLUME}  worker-ip=${WORKER_IP}  server-url=${WORKER_SERVER_URL}"
  echo "    继承 env: $(printf '%s\n' "${WORKER_ENVS[@]}" | cut -d= -f1 | tr '\n' ' ')"
}

run_worker() { # run_worker <worker_ip> <volume> <server_url>(env 取全局 WORKER_ENVS)
  local ip=$1 volume=$2 server_url=$3
  local env_flags=() e
  for e in "${WORKER_ENVS[@]}"; do env_flags+=(-e "$e"); done
  # /root/.docker 只读挂进来:拉推理镜像的是 worker 容器里的 docker-py(不是宿主机
  # docker CLI),它只认**自己文件系统**里的 ~/.docker/config.json。不挂的话,宿主机
  # 哪怕 docker login 过,私有 registry 照样 403 "no X-Auth-Token or Authorization
  # header"(2026-09-10 昇腾 SWR 就是这么翻的车)。挂上以后 worker 重建也自动继承登录态,
  # 不必再跑 fleet-docker-login.sh 往容器里补。宿主机没这目录时 docker 会建个空的,等同现状。
  mkdir -p /root/.docker
  docker run -d --name "$WORKER_NAME" \
    "${env_flags[@]}" \
    --restart=unless-stopped --privileged --network=host \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --volume /root/.docker:/root/.docker:ro \
    --volume "${volume}:/var/lib/gpustack" \
    --volume /nfs-models:/nfs-models --volume /nfs-output:/nfs-output \
    --volume /nfs-data:/nfs-data \
    --volume "${PROD_MODELS_MP}:${PROD_MODELS_MP}:ro" \
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

# DNS 自愈:节点(尤其重装/新开机)偶发 systemd-resolved 没把 DNS 写进 resolv.conf
# (resolvectl dns 全空)→ apt update / docker pull 全挂在 "Temporary failure resolving"。
# 先探一次,不通就 restart systemd-resolved 再探,给两次重启机会,仍不通才 die。
# shellcheck disable=SC2120  # 探测域名是可选参数,调用方故意用默认值(不传参)
ensure_dns() { # ensure_dns [探测域名]
  local host=${1:-mirrors.aliyun.com} i
  for i in 1 2 3; do
    if getent hosts "$host" > /dev/null 2>&1; then
      [ "$i" -gt 1 ] && echo "    DNS 已恢复(第 $((i - 1)) 次重启 systemd-resolved 后可解析 ${host})"
      return 0
    fi
    [ "$i" -eq 3 ] && break
    echo "    ⚠️ DNS 解析 ${host} 失败,重启 systemd-resolved(第 ${i}/2 次)..."
    systemctl restart systemd-resolved || true
    sleep 3
  done
  die "DNS 不可用:两次重启 systemd-resolved 后仍无法解析 ${host}" \
    "手查: resolvectl dns(Global/各 Link 是否拿到 DNS 地址,应类似 100.125.0.25)" \
    "手修: systemctl restart systemd-resolved;或临时 echo 'nameserver 100.125.0.25' > /etc/resolv.conf" \
    "根因多为网卡 DNS 未下发,对照既有节点 resolvectl dns 的地址核对"
}

# 基础环境三步(install 与 setup-base 共用):apt 基础包 / NFS 挂载 / nvidia-toolkit。
# step 计数走全局 STEP_NO,调用方把这 3 步计入自己的 STEP_TOTAL。
base_env_steps() {
  step "apt 基础包(逐个装,避免一包失败全中止)"
  # shellcheck disable=SC2119  # 用默认探测域名,无需传参
  ensure_dns   # 先修 DNS,否则 apt update 会挂在 Temporary failure resolving
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
  # 12 -> 10:indextts2 / bernini 两个镜像预载步骤已下线(见下方注释)
  # 10 -> 11:新增 breeze-tts 预载
  # 11 -> 12:新增 yue2 预载
  STEP_TOTAL=12

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

  # [2026-08-25 下线] indextts2 / bernini 不再默认预载:
  #   - indextts2:其能力已由 vllm-omni 承接(indextts-2 模型跑在 vLLMOmni 后端上)
  #   - bernini:已下线,且它停留在 cu128 base(requires-python >=3.11,<3.12,
  #     与 cu130 base 的 Python 3.12 冲突,见 gpustack 仓
  #     docs/cu130-py312-upgrade-2026-08-24.md)
  # 两者各约 10G,50 台每台都 load 是纯浪费(磁盘 + NFS 带宽 + 时间)。
  # 变量与 upgrade-engine 分支仍保留,需要时可手动:
  #   ./lx2v-node.sh upgrade-engine --engine indextts|bernini [--offline]

  step "镜像:acestep 引擎(NFS tar 优先,无则在线拉)"
  # 文生音乐整卡单实例,全节点预载即可被调度落任意空闲卡
  fetch_image_prefer_tar "$ACESTEP_IMAGE" "$ACESTEP_TAR"

  step "镜像:vllm-omni 引擎(soft 预载:有则装上供手测,缺则告警不阻塞)"
  # vllm-omni 尚未在 GPUStack 注册后端(P3 前不会被调度),用 soft 预载:有镜像的
  # 节点自动装上(供 docker run 手测全模型语音),缺镜像的节点不卡 install。
  # 注册进 gpustack 后,把这里的 soft 去掉即回归"预载即要求"。
  fetch_image_prefer_tar "$VLLM_OMNI_IMAGE" "$VLLM_OMNI_TAR" soft

  step "镜像:breeze-tts 引擎(soft 预载:有则装上,缺则告警不阻塞)"
  # 已注册后端 BreezeTTS,会被调度落任意空闲卡,故与 acestep 同样全节点预载。
  # tar 约 8.9G(镜像 25.9G,压缩层约三分之一),与 lightx2v/acestep 同量级。
  # soft 的理由是它新接入:缺镜像的节点在实例创建时由 runtime 现拉(慢但可用),
  # 不该因此卡住整个 install。
  fetch_image_prefer_tar "$BREEZE_IMAGE" "$BREEZE_TAR" soft

  step "镜像:yue2 引擎(soft 预载;在线优先、NFS tar 兜底,与其他引擎相反)"
  # 已注册后端 YuE2,会被调度落任意空闲卡,故全节点预载。顺序反过来的原因:它与上面刚装的
  # vllm-omni(step 9)共用 52 层基座,在线拉只下 ~160MB app 层(gpu41 实测 11s);tar 优先
  # 则要从 NFS 整读一份基座(~11.5G),fleet 并发时还会互抢 NFS。vllm-omni 若没装上(soft),
  # 在线拉会下整份基座,仍比读 tar 不差。只有 --offline 或拉不到时才读 tar。
  # 放在 if 条件里:拉取失败不触发 set -e / ERR trap,保持 soft。
  if [ "$OFFLINE" -eq 0 ] && docker_pull_retry "$YUE2_IMAGE"; then
    echo "    当前镜像: $(docker images --format '{{.ID}}  {{.Repository}}:{{.Tag}}' | grep -F "${YUE2_IMAGE#*/}" | head -1)"
  else
    fetch_image_prefer_tar "$YUE2_IMAGE" "$YUE2_TAR" soft
  fi

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
  STEP_TOTAL=5
  docker inspect "$WORKER_NAME" > /dev/null 2>&1 || die "本机没有 ${WORKER_NAME} 容器(全新节点请用 install)"

  step "读取现有 worker 配置(GPUSTACK_* env / 卷 / server-url / IP 全部原样保留)"
  ensure_mount "$NFS_PROD_MODELS_EXPORT" "$PROD_MODELS_MP" "$PROD_MODELS_OPTS"
  collect_worker_config

  step "拉取/加载新 gpustack 镜像"
  fetch_image "$GPUSTACK_IMAGE" "$GPUSTACK_TAR"

  step "重建 worker 容器"
  docker stop "$WORKER_NAME" && docker rm "$WORKER_NAME"
  run_worker "$WORKER_IP" "$WORKER_VOLUME" "$WORKER_SERVER_URL"

  step "验证注册"
  verify_worker

  step "回收悬空镜像"
  prune_dangling
  finish
}

# mount-nfs:只补 NFS 挂载(含 fstab)与软链,不碰任何容器。已挂的跳过,可反复跑。
cmd_mount_nfs() {
  STEP_TOTAL=1
  step "挂载 NFS + 软链"
  ensure_nfs
  finish
}

# rebuild-worker:只为改挂载而原样重建 worker,**不拉镜像**。upgrade-gpustack 会先拉
# 最新 gpustack:lx2v-dev —— tag 若在上次全队升级后动过,"加个挂载"就会顺手把全队升了版本。
cmd_rebuild_worker() {
  parse_flags "$@"
  STEP_TOTAL=4
  docker inspect "$WORKER_NAME" > /dev/null 2>&1 || die "本机没有 ${WORKER_NAME} 容器(全新节点请用 install)"

  step "宿主机 NFS 挂载(worker 挂 ${PROD_MODELS_MP} 之前它必须已是挂载点)"
  ensure_nfs

  step "读取现有 worker 配置(镜像 / GPUSTACK_* env / 卷 / server-url / IP 全部原样保留)"
  local image_id
  image_id="$(docker inspect "$WORKER_NAME" --format '{{.Image}}')"
  # tag 仍指向在跑的镜像就用 tag(docker ps 可读);已被后来的 pull 挪走则钉镜像 ID
  if [ "$(docker image inspect "$GPUSTACK_IMAGE" --format '{{.Id}}' 2>/dev/null || true)" != "$image_id" ]; then
    echo "    ⚠️ 本地 ${GPUSTACK_IMAGE##*/} 已不是 worker 在跑的镜像,按镜像 ID 重建以保持版本不变"
    GPUSTACK_IMAGE="$image_id"
  fi
  echo "    image=${GPUSTACK_IMAGE}"
  collect_worker_config

  step "重建 worker 容器"
  docker stop "$WORKER_NAME" && docker rm "$WORKER_NAME"
  run_worker "$WORKER_IP" "$WORKER_VOLUME" "$WORKER_SERVER_URL"

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
    acestep)   img="$ACESTEP_IMAGE";   tar="$ACESTEP_TAR" ;;
    vllm-omni) img="$VLLM_OMNI_IMAGE"; tar="$VLLM_OMNI_TAR" ;;
    bernini)   img="$BERNINI_IMAGE";   tar="$BERNINI_TAR" ;;
    breeze)    img="$BREEZE_IMAGE";    tar="$BREEZE_TAR" ;;
    yue2)      img="$YUE2_IMAGE";      tar="$YUE2_TAR" ;;
    vllm-backport) img="$VLLM_BACKPORT_IMAGE"; tar="$VLLM_BACKPORT_TAR" ;;
    *) die "未知引擎: $ENGINE_SEL" \
         "--engine 只支持 lightx2v | indextts | acestep | vllm-omni | bernini | breeze | yue2 | vllm-backport" ;;
  esac
  STEP_TOTAL=3
  step "当前引擎镜像(${ENGINE_SEL})"
  local old_id
  old_id="$(docker images --format '{{.ID}}' "$img" | head -1 || true)"
  echo "    old=${old_id:-<无>}  目标 tag: ${img##*/}"
  # backport 的 tag 必须和 GPUStack 模型配置里的一致,否则拉了也用不上(见文件头注释)
  if [ "$ENGINE_SEL" = "vllm-backport" ] && [ "${img##*:}" = "arm64-sm80-latest" ]; then
    echo "    ⚠️ 用的是可变 tag arm64-sm80-latest。backport 的版本权威来源是**模型 YAML 里的"
    echo "       image_name**(官方 vLLM 后端没有 version_configs),若那里钉的是时间戳 tag,"
    echo "       本次拉取对起实例毫无帮助 —— 用 VLLM_BACKPORT_IMAGE=<完整镜像:tag> 显式指定"
  fi

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

  step "回收悬空镜像"
  prune_dangling
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
  docker images --format '  {{.ID}}  {{.Repository}}:{{.Tag}}' | grep -E "gpustack|lightx2v|indextts|acestep|vllm-omni|vllm-backport|bernini|breeze-tts|yue2" || true
  echo "--- NFS:"
  mountpoint -q /nfs-models && echo "  /nfs-models OK" || echo "  /nfs-models 未挂载"
  mountpoint -q /nfs-output && echo "  /nfs-output OK" || echo "  /nfs-output 未挂载"
  mountpoint -q "$PROD_MODELS_MP" && echo "  ${PROD_MODELS_MP} OK(prod 模型,只读)" || echo "  ${PROD_MODELS_MP} 未挂载"
  docker inspect "$WORKER_NAME" --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null \
    | grep -qx "$PROD_MODELS_MP" && echo "  worker 已挂 ${PROD_MODELS_MP}" \
    || echo "  worker 未挂 ${PROD_MODELS_MP}(需 rebuild-worker)"
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

cmd_prepare_transfer() { # 步数须与下面 step 调用数一致,否则进度显示成 [step 6/5]
  parse_flags "$@"
  # 7 -> 4:indextts2/bernini 已下线,且"统一 pull"那步并入各镜像的 sync
  # 4 -> 5:新增 breeze-tts;5 -> 6:新增 vllm-backport;6 -> 7:新增 yue2
  STEP_TOTAL=7
  # tar 必须落在共享 NFS 上;未挂载时 mkdir -p 会静默建本地目录,
  # 大 tar(引擎/indextts 各 ~10G)写进根盘且其他节点拿不到
  mountpoint -q /nfs-models || die "/nfs-models 未挂载,拒绝把 tar 写到本地盘" \
    "先挂 NFS(fstab 两行 + mount -a,见 install 的 ② 或全记录 §4.2)再重试"
  mkdir -p "$TRANSFER_DIR"

  # 必需镜像的同步失败先攒着,末尾统一 die —— 中途 die 会漏掉后面的步骤(见 vllm-omni
  # 那步的注释)。空 = 全部成功。
  SYNC_FAILED_REQUIRED=""

  # [2026-08-25] indextts2 / bernini 已下线,不再拉取与 save(各约 10G)。
  # NFS 上它们的旧 tar 未删除,如需临时恢复:
  #   docker pull --platform linux/arm64 "$INDEXTTS_IMAGE" && save_tar_if_changed "$INDEXTTS_IMAGE" "$INDEXTTS_TAR"
  #
  # 原先是"先统一 pull 四镜像,再逐个 save"。现在改为每镜像一步 sync_image_to_nfs:
  # 先查远端指纹(不拉取),未变则连 pull 都跳过 —— 省的是每次都要走一遍的 pull。

  step "同步 gpustack tar(远端未变则跳过 pull+save;必须 > 重定向,不能 -o,坑#5)"
  # --platform:gpustack 镜像是多架构 manifest;containerd 镜像存储下,不带 --platform
  # 的 docker save 会尝试导出整个 manifest list(含未拉的 amd64)→ "content digest
  # not found"。指定 arm64 只导该平台,免去"先拉 amd64 占本地"的前置步骤。
  sync_image_to_nfs "$GPUSTACK_IMAGE" "$GPUSTACK_TAR" --platform linux/arm64

  step "同步 lightx2v tar(~8G,远端未变则跳过 pull+save)"
  sync_image_to_nfs "$ENGINE_IMAGE" "$ENGINE_TAR"

  step "同步 acestep tar(~8G,远端未变则跳过 pull+save)"
  sync_image_to_nfs "$ACESTEP_IMAGE" "$ACESTEP_TAR"

  step "同步 vllm-omni tar(~11G;必需)"
  # [2026-09-07] 从 soft 提为必需。原来的理由「vllm-omni 未注册后端(不被调度)」已经
  # 过时:sensenova-u1.5 / qwen-image-edit / hunyuan-image-3 三个生图模型现在都跑在它
  # 上面。而且**模型的多图输入能力是登记在引擎里的**(vllm-omni 的
  # diffusion/model_metadata.py,2026-09-05 那笔 [Bugfix][SenseNova] 才加上 SenseNova
  # 的 9 张;没有那条登记时 serving 层按 dataclass 默认值当成"最多 1 张",在 HTTP 边界
  # 拒掉所有多图编辑)。tar 停在更早的版本 + upgrade 时 pull 被限流回退,那台节点就会
  # 悄悄退回"只认 1 张",而验收 ⑦ 只查 gpustack-worker 的 label,发现不了。
  #
  # 记录失败而不是当场 die:中途 die 会让下面的 breeze-tts 与 amd64 tag 恢复整段不执行
  # (这个坑下面那条注释记过一次)。所有步骤跑完后统一非 0 退出。
  sync_image_to_nfs "$VLLM_OMNI_IMAGE" "$VLLM_OMNI_TAR" \
    || SYNC_FAILED_REQUIRED="${SYNC_FAILED_REQUIRED} vllm-omni"

  step "同步 breeze-tts tar(~8.9G;soft:拉不到只告警,不阻塞其余必需 tar)"
  # tar 存的是压缩层,约为镜像的三分之一:镜像 25.9G → tar 8.9G,与 lightx2v(8.1G)、
  # acestep(8.2G)同量级。soft 的理由不是体积而是它新接入、非必需,ACR 抖一下
  # 不该让前面几个 tar 白同步。
  #
  # 仍然是 soft(与上面的 vllm-omni 分道):它没有"能力登记在引擎里、装旧版会静默改变
  # 接口行为"的那个问题。哪天它开始承接线上模型,照 vllm-omni 那样提为必需即可 ——
  # 把下面的 echo 换成 SYNC_FAILED_REQUIRED 追加。
  sync_image_to_nfs "$BREEZE_IMAGE" "$BREEZE_TAR" \
    || echo "    ⚠️ (soft) breeze-tts 同步失败,跳过其 tar(不影响其余镜像)"

  step "同步 yue2 tar(~11.5G;soft:拉不到只告警,不阻塞其余必需 tar)"
  # tar 是 docker save 的整镜像,含与 vllm-omni 相同的 52 层基座,独有部分只有 ~160MB。
  # 仍然出 tar:它是 --offline 节点唯一的来源;在线节点 install/upgrade-engine 都会先在线拉。
  sync_image_to_nfs "$YUE2_IMAGE" "$YUE2_TAR" \
    || echo "    ⚠️ (soft) yue2 同步失败,跳过其 tar(不影响其余镜像)"

  step "同步 vllm-backport tar(~11G;soft:仅在显式指定 tag 时出包)"
  # 与其他引擎不同,它默认**不出 tar**:tar 名按 tag 派生(见变量定义处),不显式给
  # VLLM_BACKPORT_IMAGE 就会拿 latest 去出一个 vllm-backport-arm64-sm80-latest.tar,
  # 而现网模型配置钉的是时间戳 tag,那个 tar 谁也用不上,白占 11G NFS。
  # 要分发时:
  #   VLLM_BACKPORT_IMAGE=.../vllm-backport:arm64-sm80-20260915-0047-85d0e70c \
  #     ./lx2v-node.sh prepare-transfer
  if [ "${VLLM_BACKPORT_IMAGE##*:}" = "arm64-sm80-latest" ]; then
    echo "    跳过:未显式指定 tag(默认 latest 出的 tar 与现网钉的时间戳 tag 不匹配)"
    echo "    需要时: VLLM_BACKPORT_IMAGE=<完整镜像:tag> $0 prepare-transfer"
  else
    sync_image_to_nfs "$VLLM_BACKPORT_IMAGE" "$VLLM_BACKPORT_TAR" \
      || echo "    ⚠️ (soft) vllm-backport 同步失败,跳过其 tar(不影响其余镜像)"
  fi

  # 放在所有 tar 同步之后:这一步只修本机 tag,与出 tar 无关。
  # 早先它跟在 gpustack sync 后面,ACR 一抖就在 step 1/4 整段中止,后三个 tar 全没出。
  if [ "$(uname -m)" = "x86_64" ] && docker image inspect "$GPUSTACK_IMAGE" >/dev/null 2>&1; then
    # 238 的 server 容器与本 tag 同名:上面 --platform arm64 的 pull 已把本地 tag
    # 指向 arm64 镜像,不恢复的话之后跳过 pull 直接 docker run 会 exec format error
    echo "    恢复本地 amd64 tag(server 与本 tag 同名)..."
    docker_pull_retry --platform linux/amd64 "$GPUSTACK_IMAGE" \
      || die "amd64 tag 恢复失败(已重试 3 次);四个 tar 均已同步完成,只差本机 tag" \
           "本机 ${GPUSTACK_IMAGE} 现在指向 arm64 镜像,238 上直接 docker run 会 exec format error" \
           "网络恢复后单独补一条:docker pull --platform linux/amd64 ${GPUSTACK_IMAGE}"
  fi

  cp -f "$0" "${TRANSFER_DIR}/lx2v-node.sh" && chmod +x "${TRANSFER_DIR}/lx2v-node.sh"
  echo "    脚本自身已同步到 ${TRANSFER_DIR}/lx2v-node.sh(已挂 NFS 的节点可直接执行)"
  echo "    提示:nvidia-repo/ 两件套如缺,在既有 GPU 节点执行:"
  echo "      mkdir -p ${NVIDIA_REPO_DIR} && cp /etc/apt/sources.list.d/nvidia-container-toolkit.list \\"
  echo "         /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ${NVIDIA_REPO_DIR}/"

  # 必须非 0 退出:回退路径的正确性全靠这些 tar 是新的。这里放行的话,后面各节点
  # upgrade 时 pull 一被限流就静默 load 旧 tar,而且 ✅/FAIL=0 全绿 —— 2026-09-06
  # 那 13 台就是这么来的(手册 §⑦)。
  [ -z "$SYNC_FAILED_REQUIRED" ] || die "必需镜像 tar 同步失败:${SYNC_FAILED_REQUIRED# }" \
    "其余 tar 与本机 tag 已处理完,只差这些" \
    "别直接往下走 ⑤:各节点 pull 失败会回退到 NFS 上的旧 tar,静默装旧版且验收全绿" \
    "网络恢复后重跑 prepare-transfer(未变的镜像会跳过 pull+save,很快)"
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
  mount-nfs)        cmd_mount_nfs "$@" ;;
  rebuild-worker)   cmd_rebuild_worker "$@" ;;
  upgrade-engine)   cmd_upgrade_engine "$@" ;;
  clean)            cmd_clean "$@" ;;
  status)           cmd_status "$@" ;;
  prepare-transfer) cmd_prepare_transfer "$@" ;;
  *) usage ;;
esac
