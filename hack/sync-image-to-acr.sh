#!/usr/bin/env bash
#
# 把 GPUStack 镜像(多架构)同步到内网阿里云 ACR,供 server/worker 快速拉取。
#
# 背景:部署机所在 VPC 连 Docker Hub 不通、连 quay.io 慢且不稳(尤其 ARM 的
# dev-gpustack-a100-0001 经常拉到一半断)。因此用能稳连 quay 的机器
# (dev-gpustack-manager)把镜像转运进 ACR,其余节点一律从 ACR 内网秒拉。
#
# 现阶段:纯镜像镜像化(quay 官方 -> ACR)。等 Phase A 代码落地后,改为
# 「基于官方基础层叠加我们改动的文件」再推(见 pack/Dockerfile.acr 的 TODO)。
#
# 用法(先 docker login 到 ACR,再跑):
#   docker login crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com
#   ./hack/sync-image-to-acr.sh                 # 默认 quay latest -> ACR gpustack:latest(多架构)
#   SRC=quay.io/gpustack/gpustack:v0.7.0 ./hack/sync-image-to-acr.sh v0.7.0
#
set -euo pipefail

SRC="${SRC:-quay.io/gpustack/gpustack:latest}"
ACR_NS="${ACR_NS:-crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly}"
DEST_TAG="${1:-latest}"
DEST="${ACR_NS}/gpustack:${DEST_TAG}"
AUTHFILE="${AUTHFILE:-${HOME}/.docker/config.json}"

if ! command -v skopeo >/dev/null 2>&1; then
  echo ">> 未装 skopeo,正在安装..."
  apt-get update && apt-get install -y skopeo
fi

if [ ! -f "${AUTHFILE}" ]; then
  echo "!! 找不到 ${AUTHFILE},请先: docker login ${ACR_NS%%/*}" >&2
  exit 1
fi

echo ">> 多架构转运: ${SRC}  ->  ${DEST}"
skopeo copy --all --retry-times 5 \
  --dest-authfile "${AUTHFILE}" \
  "docker://${SRC}" "docker://${DEST}"

echo ">> 完成。各节点拉取:  docker pull ${DEST}"
