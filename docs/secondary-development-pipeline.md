# 二次开发与自定义出包流水线设计文档

> 适用对象:在本仓 fork 基础上做二次开发(尤其是**引入新推理引擎**),并需要把成果打成自有镜像/制品发布的团队。
>
> 本文是**设计文档**,只描述方案与决策依据,不包含最终落地代码。落地前请先确认文末「待确认问题」。

---

## 1. 背景与目标

需求是:在 GPUStack 上**引入一个新的推理引擎**,并能稳定地**出包发布**到自有镜像仓库(如 Docker Hub + 阿里云 ACR)。

参考了同目录另一个项目的工作流 `new-api/.github/workflows/docker-image-ovaijisuan.yml`,它的形态是:计算自定义版本号 → 多架构原生构建并推送 → 合并多架构 manifest → 创建 GitHub Release,并同时推送到 Docker Hub 与 ACR。这套「外壳」可借鉴,但**不能整体照搬**(原因见 §4.2)。

目标:

1. 明确「引入新引擎」在 GPUStack 代码中**到底要改哪里**,以及**要不要重打主镜像**。
2. 设计一条**不污染上游 CI、可独立维护**的 fork 出包流水线。
3. 给出端到端落地步骤清单与风险点。

---

## 2. 关键认知:引擎是「容器」,不是进程内代码

这是决定整个方案的前提。

- GPUStack 的 worker **不在自身进程内加载推理引擎**。它通过 `gpustack-runtime` 的 deployer(`WorkloadPlan` / `create_workload`)**拉起一个独立的引擎容器**。
- 内置引擎(vLLM、SGLang、MindIE、VoxBox)的**容器镜像来自外部 PyPI 包 `gpustack-runner`**(`worker/backends/base.py` 里的 `list_backend_runners`),不是在本仓 Dockerfile 里编译的。
- `pack/Dockerfile` 构建出来的 gpustack 主镜像,是**控制平面 + worker 编排核心 + 内置组件**(Higress 网关、operator、Prometheus/Grafana、PostgreSQL),**并不包含**各推理引擎本体。引擎镜像在运行期按需拉取。

**推论:出包要不要重打 gpustack 主镜像,取决于你改的是「编排核心代码」还是只是「引擎镜像本身」。** 这把「引入新引擎」拆成了两条路径(§3)。

---

## 3. 引入新引擎的两条路径

### 3.1 路径 A — 自定义后端(Custom Backend):免改 gpustack 码、免重打主镜像

**机制**

- 在 UI/API 注册一条 `InferenceBackend` 记录(`schemas/inference_backend.py`),核心字段在 `version_configs` 里:
  - `image_name`:你的引擎 Docker 镜像
  - `run_command` / `entrypoint`:启动命令与入口
  - `parameter_format`:`space`(`--key value`)或 `equal`(`--key=value`)
  - `env`:环境变量
- 运行期由 `worker/backends/custom.py` 的 `CustomServer` 负责拉起这个镜像。
- 调度的资源适配由 `policies/candidate_selectors/custom_backend_resource_fit_selector.py` 处理。
- `BackendEnum.CUSTOM` 已经是一等枚举值,无需新增。

**出包影响**

- **不需要碰 gpustack 主镜像流水线。** 你要做的只是:**单独构建并推送「你自己的引擎镜像」** 到一个 worker 节点可达的镜像仓库。
- 主镜像可以继续直接用上游官方镜像。

**适用场景**:绝大多数「想跑一个新引擎 / 新引擎版本 / 定制启动参数」的需求。**强烈建议优先走这条路。**

### 3.2 路径 B — 内置后端(Built-in Backend):改 gpustack 源码 + 重新出包

只有当你需要把引擎做成像 vLLM 那样的**一等公民**(深度的资源估算、调度打分、特殊参数处理、catalog 兼容性检查)时才走这条。涉及改动点:

| 改动位置 | 作用 |
|---|---|
| `gpustack/schemas/models.py` → `BackendEnum` | 新增枚举成员 |
| `gpustack/worker/backends/<engine>.py` | 新增 `InferenceServer` 子类(参考 `vllm.py` / `sglang.py`),实现启动/健康检查/参数拼装 |
| `gpustack/worker/serve_manager.py` | 在分发逻辑里接上新 backend |
| `gpustack/policies/candidate_selectors/<engine>_resource_fit_selector.py` | 资源适配(显存/层数估算) |
| `gpustack/scheduler/scheduler.py` | 注册新的 candidate selector |
| 引擎容器镜像来源 | 通过 `gpustack-runner` 提供,或在 `community-inference-backends` 的 yaml 中声明(构建期由 `hack/install.sh` 的 `make_community_backends()` 内嵌进 `gpustack/assets/`) |
| 代码生成(若改了带 client 的 schema)| `make generate` 重新生成 `gpustack/client/generated_*.py` |

**出包影响**:必须重新跑 `make build`(wheel)和/或 `make package`(主镜像)——这正是本文流水线设计要解决的部分。

> 决策建议:**先用路径 A 验证引擎可跑通**,只有在确实需要深度调度/资源估算时再升级到路径 B。

---

## 4. 出包流水线设计

### 4.1 上游现有出包机制(基线)

构建统一走 `Makefile → hack/*.sh`,全部用 `uv`。两类制品:

- **Wheel**:`make build`(`hack/build.sh`)。顺序:`prepare_dependencies`(`hack/install.sh`:下载 UI、拷 static、内嵌 community backends)→ `set_version`(把 git 版本写入 `gpustack/__init__.py` 与 `pyproject.toml`)→ `uv build`。产物在 `dist/`。`[tool.hatch.build]` 会把 `gpustack/ui` 与 `gpustack/third_party` 打进 wheel。
- **镜像**:`make package`(`hack/package.sh`)。`docker buildx` + `pack/Dockerfile`,多架构。关键环境变量:`PACKAGE_TAG`、`PACKAGE_ARCH`、`PACKAGE_PUSH`、`PACKAGE_UI_DOWNLOAD`、`PACKAGE_NAMESPACE`/`PACKAGE_REPOSITORY`。

上游 GitHub Actions:

- `.github/workflows/ci.yml`:构建 wheel,tag 触发时发 GitHub Release + `make publish-pypi`。
- `.github/workflows/pack.yaml`:构建主镜像。特点:**per-arch 原生 runner(amd64 / arm64)分别构建 → push-by-digest → 单独 job 合并 manifest**;用 `gpustack/build-cache` registry 缓存 + `buildkit-cache-dance` 缓存 uv 依赖;`timeout-minutes: 360`;还顺带打 Helm chart。

> 注意外部资产依赖:UI 来自腾讯云 COS 预编译包(按 release tag 对齐,否则回退 `latest`);community backends 来自 `gpustack/community-inference-backends`。fork 构建时这两处的可达性/版本对齐要保证(见 §6)。

### 4.2 参考 new-api 工作流:可借鉴 vs 不可照搬

参考文件 `docker-image-ovaijisuan.yml` 的结构:

```
prepare(算版本号) → build_single_arch(amd64/arm64 原生构建并 push 带 -arch 后缀的 tag)
                  → create_manifests(imagetools 合并出无后缀 tag)
                  → create_release(gh release)
```

| 可借鉴(直接采纳) | 不可照搬(gpustack 特有约束) |
|---|---|
| 自定义版本号:`<上游tag>-<flavor>-<date>-<sha>` | new-api 是轻量 Go 应用,几分钟构建完;gpustack 主镜像含 vLLM/CUDA/Higress,单架构可能数十分钟,**必须保留 registry build-cache + 依赖缓存**,否则每次全量重建 |
| 多架构原生 runner 分别构建(amd64 用 `ubuntu-latest`,arm64 用 `ubuntu-24.04-arm`) | new-api 用 `tag-amd64` / `tag-arm64` 后缀再 `imagetools create` 合并;gpustack 上游用 **push-by-digest + digest 合并**,对大镜像更稳,建议沿用上游 `pack.yaml` 的 digest 方式 |
| 同时推 Docker Hub + ACR(国内拉取友好) | gpustack 构建需要 `--allow network.host`、`--allow security.insecure`、`shm-size 16G`、`ulimit nofile=65536`、磁盘清理(`maximize-docker-build-space`)等特殊配置,不能用 new-api 那种裸 `build-push-action` |
| `workflow_dispatch` + tag 触发 | gpustack 还需透传 `build-args`(`PYTHON_VERSION`、`GPUSTACK_RUNTIME_DOCKER_MIRRORED_NAME_FILTER_LABELS`、`UI_DOWNLOAD`)|

**结论**:fork 流水线 = **new-api 的「外壳」(自定义版本号 + 双仓库 tag + release)** 套在 **上游 `pack.yaml` 的「内核」(特殊 buildx 配置 + 缓存 + digest 合并)** 之上。

### 4.3 fork 流水线设计原则

1. **不改上游 `pack.yaml` / `ci.yml`**,而是**新增一个独立 workflow**(如 `.github/workflows/pack-fork.yaml`),避免与上游同步冲突、避免误推官方命名空间。
2. **复用 `pack/Dockerfile` 与 `hack/` 脚本**,不另起 Dockerfile,保证与上游构建逻辑一致。
3. **命名空间/凭据全部走 fork 自己的 secrets**,默认产物 tag 带 flavor 后缀以示区分。
4. **路径 A 与路径 B 分流**:
   - 路径 A(自定义引擎镜像)→ 一条**独立、轻量**的引擎镜像构建流水线(你自己的引擎仓库或本仓 `pack/` 旁的子目录),与 gpustack 主镜像解耦。
   - 路径 B(改了 gpustack 码)→ fork 版主镜像流水线。

### 4.4 fork 主镜像流水线(路径 B)— 设计草图

> 以下为**设计示意**,非最终代码;真正落地时按 §4.2 内核对齐 `pack.yaml`。

```yaml
# .github/workflows/pack-fork.yaml(示意)
name: Pack (fork)
on:
  workflow_dispatch: {}
  push:
    tags: ["v*-<flavor>"]      # 用带 flavor 的 tag 触发,避免和上游 v*.*.* 混淆

env:
  NAMESPACE: <your-dockerhub-namespace>
  REPOSITORY: gpustack
  PYTHON_VERSION: "3.11"

jobs:
  prepare:        # 算版本号:<upstream>-<flavor>-<date>-<sha>
  build:          # 矩阵 amd64/arm64,native runner
    # —— 必须保留上游内核 ——
    # - maximize-docker-build-space(清磁盘)
    # - actions/cache + buildkit-cache-dance(缓存 uv 依赖,key=hashFiles('uv.lock'))
    # - registry build-cache(cache-from/to 指向你自己的 build-cache 仓库)
    # - buildx driver-opts: network=host
    # - build-push-action: allow network.host/security.insecure, shm-size 16G,
    #   ulimit nofile=65536, file=pack/Dockerfile,
    #   build-args: PYTHON_VERSION / GPUSTACK_RUNTIME_DOCKER_MIRRORED_NAME_FILTER_LABELS / UI_DOWNLOAD
    # - push-by-digest=true,导出 digest 上传 artifact
  merge:          # 下载各 arch digest → imagetools create 合并出最终多架构 tag
                  # 同时推 Docker Hub 与 ACR(两套 login + 两套 tag)
  release:        # 可选:gh release create,附 wheel/chart
```

版本号策略(沿用 new-api 思路):

```
UPSTREAM=$(git tag --sort=-version:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
VERSION="${UPSTREAM:-custom}-<flavor>-$(date -u +%Y%m%d)-$(git rev-parse --short=8 HEAD)"
```

镜像 tag 矩阵(双仓库):

```
<dockerhub-ns>/gpustack:<flavor>            # 浮动,始终最新
<dockerhub-ns>/gpustack:<VERSION>           # 不可变,可追溯
<acr-registry>/<ns>/gpustack:<flavor>
<acr-registry>/<ns>/gpustack:<VERSION>
```

所需 secrets:`DOCKERHUB_USERNAME/TOKEN`、`ACR_REGISTRY/USERNAME/PASSWORD`(命名与 new-api 对齐即可)。

### 4.5 fork 引擎镜像流水线(路径 A)— 设计草图

与主镜像**完全解耦**,轻量得多(类似 new-api 原版即可胜任):

```yaml
# 你的引擎镜像:base 引擎 + 任意定制
on: { push: { tags: ["engine-v*"] }, workflow_dispatch: {} }
jobs:
  build-push:   # buildx 多架构,直接 build-push-action
    # tags: <ns>/<your-engine>:<ver>(amd64/arm64)→ imagetools 合并
```

产出的 `<ns>/<your-engine>:<ver>` 就是路径 A 里 `InferenceBackend.version_configs[].image_name` 要填的值。

---

## 5. 镜像内资产来源(出包前必须确认可达)

| 资产 | 来源 | 谁拉取 | fork 注意点 |
|---|---|---|---|
| 前端 UI | 腾讯云 COS 预编译 tarball,按 release tag 对齐 | `hack/install.sh` `download_ui()` | tag 非 `vX.Y.Z` 时回退 `latest`;fork 自定义 tag 会走回退逻辑,需确认拿到的 UI 版本符合预期 |
| 内置引擎镜像 | `gpustack-runner`(PyPI) | worker 运行期 | 升级引擎版本通常是升 `gpustack-runner` 依赖版本 |
| 社区后端定义 | `gpustack/community-inference-backends`(git clone) | `hack/install.sh` `make_community_backends()` | 构建机需能访问该 GitHub 仓库 |
| 网关/可观测性 | `pack/Dockerfile` 的 `FROM`(Higress、operator、Prometheus、Grafana) | 镜像构建期 | 这些镜像仓库需可达 |
| 兄弟 PyPI 包 | `gpustack-runner` / `gpustack-runtime` / `gpustack-higress-plugins` | `uv sync` | 版本锁在 `uv.lock` / `pyproject.toml` |

---

## 6. 端到端落地步骤(Checklist)

**路径 A(自定义引擎,推荐先做)**

1. 准备引擎容器镜像(自写 Dockerfile,暴露 OpenAI 兼容或引擎原生 API)。
2. 用 §4.5 的轻量流水线构建并推送到自有仓库。
3. 在 GPUStack UI/API 注册 `InferenceBackend`:填 `image_name` / `run_command` / `entrypoint` / `parameter_format` / `env`。
4. 部署一个模型实例选用该 backend,验证调度与推理。
5. **无需重打 gpustack 主镜像。**

**路径 B(内置引擎)**

1. 按 §3.2 改 6 处代码;若改了带 client 的 schema,跑 `make generate`。
2. 本地 `make lint && make test` 通过。
3. 本地 `make package PACKAGE_TAG=<flavor>-dev PACKAGE_PUSH=false` 验证镜像可构建、可启动。
4. 落地 §4.4 的 `pack-fork.yaml`,配置 secrets,打带 flavor 的 tag 触发。
5. 验证多架构 manifest 与双仓库推送。

---

## 7. 风险与注意事项

- **构建体量大**:主镜像构建重(CUDA/vLLM/Higress),**务必保留缓存**,否则 CI 时间与磁盘会失控;arm64 原生 runner 可用性需确认。
- **不要推上游命名空间**:fork secrets 与 `NAMESPACE` 必须指向自有仓库,默认 tag 带 flavor 后缀。
- **UI 版本对齐**:自定义 tag 会触发 `download_ui()` 回退到 `latest`,可能与你的后端不匹配;必要时固定 UI tag 或自托管 UI 包。
- **外部依赖可达性**:COS(UI)、GitHub(community backends)、各基础镜像仓库在 CI 环境必须可访问。
- **优先 Custom 后端**:能用路径 A 解决就不要改 `BackendEnum`,避免与上游分叉、降低后续 rebase 成本。
- **版本写回**:`hack/build.sh` 会临时改 `__init__.py` / `pyproject.toml` 再 `git checkout` 还原,fork 流水线勿与之冲突。

---

## 8. 待确认问题

1. **目标引擎走路径 A 还是 B?**(是否需要深度调度/资源估算,还是只要能跑起来)
2. **发布目标仓库**:Docker Hub 命名空间?是否同时推 ACR?ACR 地址/凭据?
3. **架构范围**:只 amd64,还是 amd64 + arm64?
4. **触发方式**:仅手动 `workflow_dispatch`,还是 tag 自动触发?tag 命名规则?
5. **是否需要 wheel / Helm chart 制品**,还是只要容器镜像?
6. **UI 版本**:接受回退 `latest`,还是要固定/自托管?

> 上述确认后,再进入实现阶段(编写 `pack-fork.yaml` 及必要的代码改动)。
