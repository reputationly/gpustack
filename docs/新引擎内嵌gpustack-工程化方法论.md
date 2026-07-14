# 新引擎内嵌 GPUStack 工程化方法论（详细版）

> 基于 LightX2V（视频/图像生成）与 IndexTTS2（语音合成）两次完整落地经验总结，两条链路均已真机验证（2026-07 / 15 台 ARM×4×A100 = 60 卡集群）。
> 硬件基线：鲲鹏 ARM (aarch64) + 4×A100 40G PCIE + 256G RAM / 计算节点，x86 manager (10.0.0.238)，华为云 SFS (NFS)。
>
> 三仓分工：
> - `gpustack`（fork）：控制面（backend 注册、调度、异步门面、sweeper/janitor）
> - `LightX2V` / `index-tts`：引擎仓（推理、异步任务 API、launcher、镜像）
> - `new-api`：业务网关（提交、轮询、输入物化、OBS 上传）
>
> 关键参考文档索引见文末附录 A。

---

## 0. 全景架构

```
终端用户
    ↓
new-api (gpustackplus TaskAdaptor)
  ├─ 输入物化：base64/URL/上传文件 → NFS <root>/inputs/...
  ├─ POST /v1/videos { model, task_type, user_id, input_refs, metadata透传 }
  └─ 15s 轮询 GET /v1/videos/{id} → done 后读 nfs_path → 上传 OBS → 预签名 URL 给用户
    ↓
GPUStack 门面 (238, leader)
  ├─ 六层 input_refs 校验（防穿越/IDOR）
  ├─ least-pending 选实例 + task_id↔(instance_id, native_task_id) 亲和映射（M4a 任务表）
  ├─ poll-on-GET 状态推进（无后台轮询器）
  ├─ sweeper (5s, leader-only)：超龄失败/死亡重派/僵尸回填/QUEUED 重派
  └─ janitor (10min, leader-only)：NFS TTL + 水位驱逐 + 保护集
    ↓  引擎原生异步 API：POST /v1/tasks/{video|image|audio}/
引擎实例（计算节点容器，GPUStack 调度拉起）
  ├─ gpustack-<engine>-launcher：GPU 计数 → profiles.yaml 选型 → 起 server/torchrun → 代理 /ready /metrics
  ├─ 进程内 FIFO 队列（max ~8-10），满返 503
  ├─ 读 /nfs-models（RO 权重）+ inputs 绝对路径
  └─ 写 /nfs-output/<task_type>-<model>/YYYY/MM/DD/<user_id>/<task_id>.{mp4|png|wav}
```

**四条核心不变式**：
1. 引擎无状态可丢：task_id 在引擎内存，重启即失 → status 404 → sweeper 检测重派（每次重派换 `-r{n}` 新输出路径，防旧路径污染）。
2. 输入输出全部经 NFS 物化一次，janitor 按目录结构统一清理，引擎零改、上游零守摊。
3. 配置（profile/config json）打进引擎镜像：换能力 = 换引擎镜像，GPUStack 主镜像不动。
4. `new-api 的 NFSRoot() == gpustack 的 lightx2v_output_root`，双方启动时探测 `<root>/inputs/` 可读写，失败即 fatal，不进半可用状态。

---

## 1. 五阶段流水线与里程碑模板

| 阶段 | 产出 | 所在仓 | LightX2V 实际耗费参考 |
|---|---|---|---|
| P0 选型与 POC | A100 单机跑通 + 实验测试报告 + 判死结论 | 引擎仓 | 每模型 1-3 天 |
| P0.5 引擎内接模型 | runner/weights/infer 原生实现 + 数值对齐 | 引擎仓 | 视模型复杂度 |
| P1 引擎异步化 | 异步任务 API + /ready + FIFO（IndexTTS ~500 行） | 引擎仓 | 1-2 天 |
| P2 镜像工程化 | arm64 镜像 + launcher + CI + ACR 双 tag | 引擎仓 | 首次 2-3 天，复用后半天 |
| P3 GPUStack 内嵌 | backend 注册 + selector + 门面分支 + overlay 镜像 | gpustack fork | 首个引擎 M1-M5 共 2 天；第二个引擎（IndexTTS）~10 行级 |
| P3.5 new-api 对接 | TaskAdaptor task_type + 输入物化 | new-api | 音频物化是唯一高难点 |
| P4 部署 | 节点脚本接入 + UI 部署模型 | 运维脚本 | 单节点 16min，60 卡一天 |
| P5 验证 | 补丁评审 → 灰度 → 金丝雀 → 全量 | 全链路 | 1 天 |

**里程碑模板**（照 IndexTTS 方案 `index-tts/docs/indextts2-异步内嵌-改动方案.md:151-162`）：

| M | 内容 | 仓 | 验收标准 |
|---|---|---|---|
| M1 | 引擎任务协议：FIFO + `/v1/tasks/<kind>/*` + 后台推理线程 | 引擎仓 | curl 全链路：submit → poll → 产物落 NFS |
| M2 | GPUStack 后端登记：BackendEnum、serve_manager、selector、scheduler | gpustack | 容器拉起、模型加载不超时、/ready 变 200 |
| M3 | 门面加 task_type（routes 4 处 + config） | gpustack | 任务表接收新 task_type，分发到正确引擎路由 |
| M4 | new-api adaptor：task_type + 输入物化 + metadata 透传 | new-api | 端到端：new-api submit → 物化 → 推理 → 取件 |
| M5 | UI 面板（可选后置） | gpustack-ui | 前端可提交/试听/下载 |

建议顺序：M1 → M2/M3 并行 → M4 → M5（可选）。

---

## 2. P0 — 选型与 POC

### 2.1 测试 harness 标准（`LightX2V/scripts/smoke/test_model.sh`）

通用参数：`NAME MODEL_CLS TASK MODEL_PATH CFG PROMPT IMAGE LAST_FRAME AUDIO NEG_PROMPT OUT NP FRAMES SEED STEPS RESIZE_MODE PORT HEALTH_TO`。

流程：起容器（NP=1 → `python -m lightx2v.server`；NP>1 → torchrun）→ 等 `/health` 200（记加载时长）→ POST 提交 → 每 5s 轮询 status，同时采样显存（**多卡取所有卡最大值**）/GPU 利用率/CPU%/容器内存 → **只认 `status=completed`**。

产物防呆（`smoke_test_a100.sh` 已实装）：
- 产物 <50KB 判失败（防黑屏假绿）
- 抽第 20 帧 PNG 熵检查（防雪花）
- `ffmpeg blackdetect` 黑屏检查
- GPU 空闲预检（`SKIP_GPU_GUARD=1` 可跳过）、镜像 import preflight

### 2.2 测试纪律（血泪铁律）

1. **热态稳态**：连发 6 条丢首张取均值。冷态虚高近一倍（CUDA context + triton per-shape autotune），只用于识别坑。
2. **安静宿主**：邻居容器读权重会拖慢推理 2-4 倍，性能数据必须 `docker rm -f` 清场后测。
3. 容器一律 `--memory=240g`（cgroup 杀容器保宿主）。
4. tmux 里跑长任务；**不覆盖 NFS 上正在执行的脚本**（Stale file handle）。
5. 多维矩阵：量化（bf16/int8）× 卡数（1/4）× 分辨率/帧数，产出对比表。
6. **敢于判死并记录**：Z-Image 多卡（head 不整除+反向收益）、Wan bf16 多卡（ulysses 每 rank 复制全量权重 4×57G>256G）、VACE 720p 4卡常驻（三审终审：38G 权重+激活瞬时峰 40G 卡无解）、LTX 多卡（TP rank0 暂存 46G>40G）、TP/PP（无 NVLink 每层 2×all-reduce）。

### 2.3 实验测试报告模板

环境（GPU/CPU/NUMA/镜像 tag/起容器命令）→ 权重路径（基座/量化/蒸馏/LoRA 分列）→ A100 核心配置（注明必改项）→ 速度/显存矩阵（标注热态）→ 关键发现（瓶颈归因：compute/memory/offload）→ 部署建议（推荐配置+密度上限）→ 坑点。

### 2.4 A100 config 铁律

| 配置项 | 必须值 | 原因 |
|---|---|---|
| `attn_type` | `torch_sdpa` | flash_attn3 是 Hopper 专属直接崩；sage_attn2 在 Qwen 系（NaN INT8 注意力）出黑图 |
| `rope_type` | `torch` | 镜像无 flashinfer，默认值 `NoneType not callable` |
| MoE 多卡 | int8 only | bf16 每 rank 复制全量权重 CPU OOM |
| 多卡 | 禁 `cpu_offload` | per-rank pin 打爆 host 内存 |
| MoE 单卡 offload | `offload_granularity: model` | block 粒度换专家时 buffer 不清 → 黑屏 |
| Qwen 系 | `qwen25vl_cpu_offload: false` | 文本编码器必须留 GPU |
| 蒸馏模型 | `enable_cfg: false` | 蒸馏后 CFG 无意义且翻倍耗时 |
| 并行 | ulysses（`parallel.seq_p_size`） | 只在 attention 边界通信，适合 PCIE 无 NVLink |

int8 的价值是**显存不是速度**（torchao weight-only 在 A100 不走 INT8 tensor core）；提速靠多卡 ulysses 切序列（激活÷卡数，既提速又扛高分辨率）。

---

## 3. P0.5 — 引擎内新增模型（LightX2V support_new_model 方法论）

> 完整规范见 `LightX2V/.claude/skills/support_new_model/SKILL.md`（852 行），此处摘要供总体规划。

**原则**：不依赖 diffusers/transformers 执行核心模型，用 LightX2V 数据结构和算子重建；DiT 内部张量**无 batch 维**（`x:[seq,dim]`, `q/k/v:[seq,heads,head_dim]`）。

**标准管道**：`runner → input encoder(s) → scheduler.step_pre() → model.infer()(pre/transformer/post) → scheduler.step_post() → VAE decode → save`。

**目录约定**：
```
lightx2v/models/networks/<family>/
  model.py
  weights/{pre,transformer,post}_weights.py    # 用通用 ops(MM/norm/attention)，键名与 converter 输出匹配
  infer/{pre,transformer,post}_infer.py        # 直接 phase.linear.apply(x)，继承基族 offload/feature-cache
lightx2v/models/runners/<family>/<family>_<variant>_runner.py
configs/<model>/<profile>.json
scripts/<model>/run_*.sh                       # 无硬编码路径，路径全走变量
```

**关键规范**：
- 权重提前离线转 x2v 格式（converter 脚本），运行时不回退 diffusers；missing key 先看转换产物实际键，改 converter 不改 infer。
- CFG 串行实现（cond/uncond 两次前向），禁 `torch.cat([x]*2)` 批处理；用显式 `enable_cfg` 标志而非 scale==1 判断。
- config json 只放架构字段和稳定 profile（分辨率/fps/attn/offload/parallel）；model_cls/task/prompt/输入输出路径/CUDA device 放命令行。
- 复用既有 T5/UMT5/Qwen 编码器和 Wan/Qwen VAE 包装，别重造。

**数值对齐验证 7 步**（与上游 pipeline 对拍，存 `.pt` fixtures 比 max/mean abs diff）：预处理 → scheduler/noise（种子/timesteps/sigmas）→ pre_infer 边界 → 单块 transformer → 全 DiT → 端到端视觉。

**补丁纪律**（`LightX2V/docs/出包清单-补丁评审.md` 模式）：
- 每个补丁写清"改了什么/为什么/影响面"；**公共文件的补丁必须证明只在原本必崩路径触发，正常路径零变化**（如 P1 DefaultTensor `__getattr__`）。
- 补丁不提上游，合自库出包；分 3 个 commit：①源码补丁 ②工具+配置 ③文档。
- 可加纯日志埋点补丁（如 P10 `[latency-probe]` ×4：提交带排队数/轮询/轮询 MISS 告警/取片 MB/s），出包后用日志定位时延归因。

---

## 4. P1 — 引擎异步化改造

### 4.1 异步任务 API 契约（必须与门面 `_ENGINE_STATE_MAP` 逐字对齐）

```
POST   /v1/tasks/{video|image|audio}/  → 200 {task_id, task_status, save_result_path}
                                        | 503 队列满(QueueFullError) | 400 空文本/超长/缺参
GET    /v1/tasks/{task_id}/status      → 200 {task_id, status, save_result_path, error, error_type,
                                              created_at, completed_at}
                                        | 404 不存在（触发门面死亡重派；日志打 "status poll MISS"）
GET    /v1/tasks/queue/status          → {is_processing, current_task, pending_count, active_count,
                                          queue_size, queue_available}
DELETE /v1/tasks/{task_id}             → 取消
GET    /ready                          → 503 加载/warmup 中 | 200 就绪（GPUStack health_check_path）
GET    /health                         → 引擎进程存活（launcher 内部探测用，可 env 覆盖路径）
GET    /metrics                        → Prometheus（可选，launcher 代理）
```

状态字符串精确集合：`pending | processing | completed | failed | cancelled`（**cancelled 双 L**；门面映射 pending→ASSIGNED、processing→RUNNING、completed→DONE、failed→FAILED、cancelled→CANCELED 单 L，`gpustack/routes/videos.py:178-184`）。错一个字母 = 状态映射失效、任务假死。

### 4.2 请求体字段设计参考

**LightX2V VideoTaskRequest**（`lightx2v/server/schema.py:75-88`，32 字段）核心：`prompt / negative_prompt / image_path / last_frame_path / image_mask_path / video_path / src_video / src_mask / src_ref_images(逗号分隔多图) / audio_path / save_result_path(默认 task_id) / seed(默认随机) / infer_steps / target_video_length / target_fps / target_shape / sr_ratio / video_duration / lora_name / lora_strength / resize_mode(adaptive 等 6 种)`。ImageTaskRequest 另加 `aspect_ratio="16:9"`、`i2i_denoise_strength`。

**IndexTTS AudioTaskRequest**（`indextts/api_server/schemas.py:13-42`）：`prompt|text / spk_audio_path(必填) / emo_audio_path / emo_vector(len==8 校验) / emo_alpha(0.0-1.0) / emo_text / use_emo_text / save_result_path(必填) / task_id`；文本上限 `MAX_TEXT_LEN=5000`，空文本 400。

设计原则：输入一律 **NFS 绝对路径字段**（由门面校验后注入），不做 URL 下载（引擎 download 到本地盘无 TTL 迟早写满，这是 NFS 输入设计取代 OBS-URL 的核心原因）。

### 4.3 FIFO 队列实现要点（照抄 `indextts/api_server/`，4 文件 ~500 行）

| 要点 | 实现（file:line） |
|---|---|
| 队列上限 | `max_queue_size` 默认 8，env `INDEXTTS_MAX_QUEUE` 可配；`active = pending + processing >= max` 时抛 QueueFullError → 路由返 503（task_manager.py:59-89） |
| 锁结构 | `RLock` 保护状态 + `Condition` 通知 worker + 独立 `_processing_lock` 串行化 GPU 推理 |
| 取消 | per-task `stop_event`：PENDING 直接跳过；PROCESSING 完成后丢弃结果；`PENDING→PROCESSING` 原子转换防 DELETE 竞态 |
| 终态 | `{completed, failed, cancelled}` 不可逆 |
| 原子写 | 临时文件 `{root}.part{ext}`（**必须保留扩展名**，soundfile 等按后缀推断编码，commit 353c9de 的教训）→ `os.replace()` 同目录 inode rename → 读者永不见半截文件；异常 finally 清理 |
| warmup | FastAPI startup：`ensure_models_available()`（辅助模型离线检查）→ 加载模型（`use_cuda_kernel=False` 跳过 BigVGAN nvcc JIT 省 3-5min）→ 起 worker 线程 → `/ready` 才 200 |

### 4.4 引擎侧明确不做的事

- 不持久化任务（重启丢任务是特性，靠门面 sweeper 重派）
- 不做鉴权/多租户（门面负责）
- 不做输入清理（janitor 负责）
- 可保留同步兼容端点（如 `/v1/audio/speech`：OpenAI 兼容 input/voice/response_format + 扩展 emotion_* 字段，voice_id → `<VOICES_DIR>/<id>.wav`）方便手测

---

## 5. P2 — 镜像工程化

### 5.1 Dockerfile 分层策略

**引擎基础镜像**（`LightX2V/dockerfiles/Dockerfile_aarch64_cu128`，重编译层，很少变）：

```
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04          # 有 ARM64 官方镜像
ENV TORCH_CUDA_ARCH_LIST="8.0"                            # 只编 A100，砍 sm_90/sm_120
ENV LD_LIBRARY_PATH=/usr/local/lib/aarch64-linux-gnu:...
apt: python3.10 build-essential ffmpeg libnuma-dev ...
pip torch==2.11.0 torchvision torchaudio --index-url .../whl/cu128   # PyPI aarch64 只有 CPU 版！
sgl-kernel（FP8，失败容忍 || echo skip）
运行依赖：diffusers transformers safetensors av decord soundfile prometheus_client torchao ...
flash-attn==2.7.4.post1（FLASH_ATTN_CUDA_ARCHS="80"）
SageAttention / SpargeAttn / q8_kernels（TORCH_CUDA_ARCH_LIST="8.0"，可失败）
COPY . /opt/LightX2V && pip install -e . --no-deps
构建断言：import torch/flash_attn/sageattention 冒烟
```

**app 层**（`Dockerfile_aarch64_app`，频繁变）：只更新代码 + launcher + configs，base 层复用 → 增量拉取分钟级。

**衍生引擎镜像**（`index-tts/Dockerfile.lightx2v`，36 行范式）：

```dockerfile
ARG BASE_IMAGE=arronlee/lightx2v:arm64-cu128-a100-base    # 复用 LightX2V base（Docker Hub）
FROM ${BASE_IMAGE}
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=/app
COPY . /app
# 关键：从 base freeze torch 版本生成约束，防止 pip 装回 CPU 版
RUN pip freeze | grep -iE '^(torch|torchaudio)==' > /tmp/constraints.txt || true; \
    pip install --no-cache-dir -c /tmp/constraints.txt <运行依赖清单，显式钉版本>
EXPOSE 8000 7860
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

依赖三原则：① torch/torchaudio 约束冻结；② 冲突包显式装回兼容版（numpy 2.2.6→1.26.2 适配 numba/librosa）；③ 训练用重依赖（keras/tensorflow/tensorboard/cython）一律不装。ARM 无 wheel 的包：先找纯 Python 替代（pynini→`wetext>=0.0.9` + 改 front.py Linux 分支），再考虑源码编译（decord：ffmpeg-dev + cmake，或干脆 PyAV 回退补丁 P8）。

### 5.2 launcher 详解（内置 backend 必备）

源码：`LightX2V/deploy/gpustack-lx2v-launcher/gpustack_lx2v_launcher.py`（537 行），随 COPY 进镜像，无需编译。工作流：

1. **GPU 计数**（line 71-91）：读 `CUDA_VISIBLE_DEVICES`/`NVIDIA_VISIBLE_DEVICES`（GPUStack runtime 自动注入），无/为 all 时 `nvidia-smi --query-gpu=index`，失败默认 1。
2. **profile 匹配**（line 147-204）：三维匹配 GPU 数 × model_cls × task 提示；`--profile <name>` 精确指定；同 (GPU数, task) 多变体（如 infinitetalk 480p/720p）必须显式 pin；advisory 校验 `cfg_p_size × seq_p_size == GPU 数`。**匹配不到直接失败**（防呆：单卡模型被分 2 卡时报 `No 2-GPU variant`）。
3. **端口**：engine/metrics/master 三端口 `_free_ports()` 随机分配互不重复且 ≠ 公开端口（host network 多实例防冲突，line 460-481）。
4. **起引擎**：单卡 `python -m lightx2v.server ...`；多卡 `torchrun --nproc_per_node=N --master_addr=127.0.0.1 --master_port=<随机>`。
5. **代理**（line 315-400）：`/ready` 自答（未就绪 503 `{"ready":false}`）；`/metrics` 代理到内部 metrics 端口；其余全部转发到引擎端口。
6. **warmup**（line 403-433）：后台线程 poll 引擎 `/health`（env `LX2V_ENGINE_UP_PATH` 可覆盖）→ 200 后执行 profile 里可选的 `warmup: {endpoint, body, timeout}`（预热 triton autotune）→ 才 set_ready。
7. **生命周期**（line 507-533）：SIGTERM → engine.terminate() + `os._exit()`（不调 httpd.shutdown 防死锁）；独立线程 watch 引擎进程，异常退出即整体退出。

**profiles.yaml 结构**（`deploy/gpustack-lx2v-launcher/profiles.yaml`）：

```yaml
<model_cls>:                     # 顶级键 = model_cls
  variants:
    - name: wan2.2-t2v/int8-4card      # profile 名（--profile 引用）
      gpus: 4
      task: t2v                        # 消歧提示
      config_json: /opt/LightX2V/configs/deploy/wan22_t2v_int8_4card_a100.json  # 镜像内路径
      seq_p_size: 4                    # advisory 校验字段
      seq_p_attn_type: ulysses
      # warmup: {endpoint, body, timeout}   # 可选
```

现有 7 个 model_cls：z_image(1卡 t2i) / wan2.2_moe(4卡 t2v) / wan2.2_moe_distill(4卡 i2v) / qwen_image(1卡 t2i+i2i 两变体) / infinitetalk(4卡 s2v 两分辨率) / seedvr2(1卡 sr) / wan2.2_moe_vace(1卡 lora4step)。

### 5.3 CI 与 tag

- Runner：`ubuntu-24.04-arm`（GitHub 原生 arm64，免 QEMU）。
- **一次性种 base 到 ACR**：`pull from Docker Hub → tag → push`；之后 CI 只推 1-2G 增量层（~11min）。直接跨境推 10G base 层 60min 必超时；gha 缓存超 10G 上限反而拖慢，不用。
- 双 tag：浮动 `<engine>:arm64-a100-latest` + 不可变 `arm64-a100-$(date -u +%Y%m%d-%H%M)-$(git rev-parse --short=8 HEAD)`（`index-tts/.github/workflows/build-arm64.yml:48`）。
- workflow_dispatch 可传 base_image/dest_tag 参数；push 后自动建 GitHub Release 记录分发链。
- PAT 需勾 workflow scope（否则改 workflow 文件推不上去）。

### 5.4 权重与镜像交付

**权重**：
- 下载：`scripts/download_models.sh` —— ModelScope 主源（国内 ~50MB/s）+ hf-mirror 回退 + 断点续传 + 后台速度监控；`MODELS="wan_audio wan_i2v"` 选择性下载；下载完**完整性自检**（du -sh + 逐个 ls -lh 大文件）。
- 量化：`scripts/convert_int8.sh` —— docker 容器内跑（--memory 限制护宿主）、分块保存、`.done` 标记跳过、敏感层策略（只量化 attn/mlp；黑屏时 `--ignore-quant-keys` 跳敏感块重转）。
- 交付：`checkpoints.tar.gz` 解压到 `/nfs-models/...`，容器 RO 挂载；HF 附属模型在联网机预填 hf_cache 整目录随包（运行时 `HF_HUB_OFFLINE=1` 离线加载，日志 `All auxiliary models ready` 是成功标志）。

**镜像**：
- 节点有旧镜像：`docker pull --platform linux/arm64 <ACR>`（base 复用只拉增量）。
- 全新节点：238 上 `lx2v-node.sh prepare-transfer`（pull 三镜像 → `docker save IMG > /nfs-models/_transfer/xx.tar`）→ 节点 `docker load`。
- 坑：多架构 tag 在 x86 上直接 save 报 `content digest not found`，先 `pull --platform linux/arm64`；`docker save -o` 在 NFS 上失败还留隐藏 `.tmp-*` 半截文件占几 G，**用 shell 重定向**。

---

## 6. P3 — GPUStack 内嵌

### 6.1 路径选择

- **路径 A（Custom Backend）**：UI 注册 InferenceBackend + 自有镜像，零改源码。同步/无状态引擎优先走这条验证。
- **路径 B（内置 backend）**：**异步（有状态）引擎必须走**——submit 返回的 task_id 存在某副本内存，纯 Custom + 轮询会命中别的副本 404。内置化换来：亲和映射、least-pending、死亡重派、profile 调度、UI 原生类目。
- fork 纪律（`secondary-development-pipeline.md`）：不碰上游 `pack.yaml`/`ci.yml`，独立 `pack-acr-overlay.yml`；scheduler.py 的 fork 臂保持一条 elif 最小化（上游 churn 最猛处）。

### 6.2 backend 注册的精确写法（`gpustack/schemas/inference_backend.py:370-453`）

```python
InferenceBackend(
    backend_name=BackendEnum.LIGHTX2V.value,
    is_built_in=True,
    default_version="1.0.0",
    version_configs=VersionConfigDict(root={"1.0.0": VersionConfig(
        image_name="crpi-xzr81d0490mc3794....aliyuncs.com/reputationly/lightx2v:arm64-a100-latest",
        custom_framework="cuda",     # ← 关键：非 built_in_frameworks，
    )}),                             #   让 BackendFrameworkFilter 接受无 gpustack-runner 的 cuda 节点，
    health_check_path="/ready",      #   且 get_image_name() 直接用显式镜像名不走 runner 解析
    parameter_format=ParameterFormatEnum.SPACE,   # --key value
)
```

启动命令模板：LightX2V `gpustack-lx2v-launcher --model {{model_path}} --host {{worker_ip}} --port {{port}}`；IndexTTS `python3 -m uvicorn server:app --host {{worker_ip}} --port {{port}}`（单卡简单引擎可不要 launcher）。

### 6.3 源码改动清单（新增引擎照此办理）

**调度侧：**

| 文件 | 改动 | 备注 |
|---|---|---|
| `schemas/models.py` | `BackendEnum.XXX`；新品类才加 `CategoryEnum` | |
| `schemas/inference_backend.py` | `get_built_in_backend()` 加一条（见上） | server 启动时自动建库（日志 `Init built-in backend XXX`） |
| `worker/backends/<engine>.py`（新建） | 继承 `InferenceServer`：`_build_command_args`（launcher 命令 + `build_versioned_command_args` + 用户参数 + `extend_args_no_exist` 幂等补 --host/--port） | lightx2v.py 168 行 / indextts.py 对称 |
| `worker/serve_manager.py` | `_SERVER_CLASS_MAPPING` 加映射 | |
| `policies/candidate_selectors/<engine>_resource_fit_selector.py`（新建） | profile 表驱动固定卡数（1/2/4），不做显存估算；TTS 类整卡预订 | indextts 版仅 31 行（继承 lightx2v 版） |
| `scheduler/scheduler.py` | 一条 elif + selector_map 一条 | 保持最小 |

**新品类才需**：`scheduler/evaluator.py`（VIDEO 跳 runtime-version 检查）、`worker/runtime_metrics_aggregator.py`（跳 /metrics scrape）、`worker/model_meta.py`（类型识别）、UI 类目——**逐条 grep ~38 个 category/backend gate 点**（清单在 backend-design M3 节）。

**门面侧（第二个引擎起只需 4 处小改**，IndexTTS 实测，`routes/videos.py`）：
1. `_AUDIO_TASK_TYPES = {"tts"}` 并入白名单（line 85-88）
2. `_engine_kind()` 加分支 → 决定提交到 `/v1/tasks/audio/` 还是 video/image（line 196-201）
3. `_output_ext()` → `.wav`
4. `_model_latency()` 加该引擎默认时延

**模型目录**：`assets/model-catalog.yaml` 加条目 + `static/catalog_icons/` 图标。

**UI（gpustack-ui 仓）4 处枚举**：`modelCategoriesMap`/`categoryOptions`、`backendLabelMap`、`backendOptionsMap`、`builtInBackendLogos`。

### 6.4 门面 API 契约（复用组件，新引擎一般不动）

**POST /v1/videos**（`routes/videos.py:489-534`）：
- 字段：`model`（必填）、`task_type`（白名单 t2i/i2i/t2v/i2v/flf2v/s2v/tts，非法 400）、`user_id`（new-api 终端用户，默认 0，用于路径隔离，区别于 GPUStack principal）、`input_refs`（dict：image/last_frame/image_mask/audio/voice/emotion_audio/video/src_video/src_mask/src_ref_images → NFS 相对路径数组）、其余字段透传引擎。
- **原始路径字段禁传**（image_path/save_result_path 等直传 → 400，防 IDOR）。
- 数量限制：image/src_ref_images ≤5，其余 ≤1；image_mask 必须配单图；src_mask 必须配 src_video。

**input_refs 六层校验**（`routes/videos.py:292-346`，新输入字段自动继承）：
①拒绝绝对路径 → ②realpath 归一必须落在 `<root>/inputs/` 下（挡 `..`/软链逃逸）→ ③限 inputs 子树（防读输出）→ ④父目录名必须 == user_id（跨租户 IDOR 堵死）→ ⑤文件存在 → ⑥拼绝对路径注入引擎。

**GET /v1/videos/{id}**：poll-on-GET（无后台轮询器，new-api 15s 一次即推进状态），响应 `{task_id, status(queued/assigned/running/done/failed/canceled), model, task_type, nfs_path(仅 DONE), error, error_type}`；非 owner 且非 admin 一律 404（不泄露存在性）。

**GET /v1/videos/{id}/content**：DONE 才可取；realpath 校验用**任务行记录的 output_root**（创建时快照，运行时改配置不 404 旧结果）；流式返回。

**错误码**：400 参数/校验、404 不存在/无权、429 队列背压、503 无可用实例/引擎 5xx 透传。

**鉴权与计量**：Bearer API key → auth 中间件 → `model_allowed_for_user` 检查（未授权按 404）；任务行存 `owner_user_id`(principal) + `user_id`(终端用户) 双 ID；`ModelUsageMiddleware` 对 POST /v1/videos 记 `OperationEnum.VIDEO_GENERATION`（request_count=1，无 token）。**/v1/videos 故意不进 openai_model_prefixes**，防网关直连绕过门面。

### 6.5 sweeper / janitor 参数（调优基准）

**VideoTaskSweeper**（5s，leader-only，`server/video_task_sweeper.py`）：

| 常量 | 值 | 含义 |
|---|---|---|
| `_REQUEUE_MISS_THRESHOLD` | 3 | 实例连续 3 轮不在 RUNNING 才 requeue（防状态抖动） |
| `_LOST_DISPATCH_SECONDS` | 120 | ASSIGNED 无 native_task_id 超 2min = 门面提交中途崩，requeue |
| `_STALE_POLL_SECONDS` | 600 | 在飞任务停滞 10min，服务端主动查引擎回填 |
| `_TASK_MAX_AGE_HOURS` | 24 | 非终态超 24h 强制 FAILED(timeout) |
| 重派上限 | 5 | 每次换 `-r{n}` 新输出路径；先入库后提交（防孤儿引擎任务） |

**VideoStorageJanitor**（10min，leader-only）：TTL（`lightx2v_retention_days` 默认 7 天，按 day-dir O(days) 删）+ 水位驱逐（`storage_high_watermark` 0.85 → 删到 `low_watermark` 0.70，最旧优先）+ 保护集（非终态任务的输入/输出 day-dir——输入从 `params` 的 `*_path` 键含 spk_audio_path/emo_audio_path 等自动解析、DONE 后 6h 取件宽限、今天目录）；NFS IO 全走 `asyncio.to_thread`。

目录结构约定（janitor 依赖，新引擎必须遵守）：
```
<root>/<task_type>-<model>/YYYY/MM/DD/<user_id>/<task_id>.<ext>
<root>/inputs/<task_type>-<model>/YYYY/MM/DD/<user_id>/<gid>-<field>[-i].<ext>
```

4 个运行时可改 Config（`config/config.py` + `utils/config.py` 白名单，UI Storage Settings 页可编辑）：`lightx2v_output_root`(默认 /nfs-output) / `retention_days` / `high_watermark` / `low_watermark`。

### 6.6 安全设计要点

- **NFS 挂载信任边界在 WORKER ENV**：`worker/backends/*.py` 的 `_get_extra_mounts` 只读 worker 进程环境变量 `GPUSTACK_EXTRA_MOUNTS`（逗号分隔，host==container 路径），**绝不读 model.env**——推理容器 privileged，读租户可控的 model env 等于让租户挂任意宿主路径进特权容器。
- worker 启动示例：`docker run -e GPUSTACK_EXTRA_MOUNTS=/nfs-output,/nfs-models,/nfs-data ... gpustack worker`。
- 数据库枚举：state 列用原生 `sa.Enum`（PostgreSQL+asyncpg 类型 bind）；查询用枚举成员不用 `.value`（否则 WHERE IN 失效）。

### 6.7 GPUStack 主镜像出包（`pack/Dockerfile.acr` overlay）

- 把改动文件分层 COPY 覆盖到官方/自有 base 上（当前清单 ~25 个 .py + 迁移 + model-catalog + 图标 + `pack/ui-dist/`），秒级构建不重打 CUDA 层。**新增引擎务必把新文件加进 COPY 清单**（copy-list 漏文件是踩过的坑）。
- **迁移链自动修复**（line 77-86）：构建期探测 base 实际 alembic head → `sed` 改写自有迁移 `down_revision` → 断言链头为自有迁移 id。
- 构建断言（line 87-106）：`py_compile` 全部改动文件 + import 枚举断言 + `grep model-catalog` + `test -f ui-dist/index.html`。
- UI 集成：CI checkout gpustack-ui main → `pnpm build` → dist 进 `pack/ui-dist/`。
- CI 矩阵 amd64+arm64、registry build-cache、双 tag：`gpustack:lx2v-dev` + `gpustack:lx2v-<yyyymmdd-hhmm>-<sha8>`。

---

## 7. P3.5 — new-api 对接（gpustackplus channel，已全量落地含 TTS）

> 仓库：`/Users/reputationly/Desktop/code/api/new-api`。TTS（含 voice 音频物化）已于 commit 13844d618 实现，不再是待办。

### 7.1 两条链路

| 链路 | 位置 | 模式 |
|---|---|---|
| 视频/音频（异步） | `relay/channel/task/gpustackplus/` | TaskAdaptor + 后台 15s 轮询（`service/task_polling.go:94`） |
| 图片（同步） | `relay/channel/gpustackplus/` | 普通 Adaptor 服务端阻塞轮询（并发 32、queued 等待上限 25s） |

### 7.2 提交链路（`adaptor.go` BuildRequestBody，L152-290）

- **task_type 白名单**（L59-62）：`t2i/i2i/t2v/i2v/flf2v/tts/s2v/sr/vace` 九类；无显式指定时 `inferTaskType()` 按模型名关键字推断（`indextts→tts`、`infinitetalk→s2v`、`seedvr→sr`、`vace/flf2v/i2v/edit/t2i` 依序匹配，默认 t2v）。
- **legacyInputKeys 剥离**（L67-74）：原始输入字段（image/voice/emotion_audio/video/src_* 等）与引擎 owned 路径字段（image_path/spk_audio_path/save_result_path 等）从 metadata 中强制剥掉——路径由门面统一 dictates，new-api 不拼路径不 mkdir。**新引擎的引擎专有参数只要不在这个清单里就会原样透传**，加新路径类字段时必须同步加进清单。
- **防呆校验**：t2v/t2i 拒图；i2v/i2i/s2v 需图；flf2v 需 2 张图；tts 拒图 + 需 prompt + `ValidateAudioTextForModel` 字数上限。
- **尺寸/时长规范化**：`size`→同时发 `aspect_ratio`；`duration/seconds`→`target_video_length = seconds*16+1`（Wan 16fps 约定）；超管配了白名单时剔除 metadata 里的同维度引擎别名键（`target_video_length/num_frames/width/height/...`）。

### 7.3 输入物化（`relay/channel/gpustackplus/nfsinput/nfsinput.go`，全 task_type 共享）

- 每类一个 materialize 函数：`materializeVideoInputs`（t2i/i2i/t2v/i2v/flf2v）、`materializeTTSInputs`（voice 必填 + emotion_audio 可选，L532-558）、`materializeS2VInputs`、`materializeSRInputs`、`materializeVACEInputs`（src_video + src_mask + ≤5 参考图）。失败即 `Cleanup()` 回滚已写文件 → 400 skip-retry。
- 路径：`inputs/<task_type>-<sanitizedModel>/YYYY/MM/DD(UTC)/<user_id>/<gid>-<field>[-i].<ext>`，与门面/janitor 约定一致；模型名 sanitize 保留 `[A-Za-z0-9._-]`。
- 输入形式：HTTP(S) URL（**SSRF 校验**：私网 IP/DNS 过滤 + host 白名单 + 禁 3xx 跟随，30s 超时）/ data-uri / 裸 base64。
- 安全防呆：按字段类别做 **magic bytes 文件头校验**（图片 PNG/JPG/WebP/GIF、音频 WAV/MP3/AAC/Ogg/FLAC/M4A、视频 MP4/MOV/WebM/MKV 等，挡改后缀）；per-model 大小上限（`AudioRefAudioMaxBytesForModel` / `VideoMaxInputBytesForModel`，`common/media_model_config.go`）。

### 7.4 轮询与取件

- 状态映射（`ParseTaskResult`，L622-640）：`queued/assigned→Queued`、`running/processing→InProgress`、`done/completed→Success`（读 `nfs_path` 存 TaskInfo）、`failed/cancelled→Failure`、未知→保持 Queued 继续轮。
- 落盘（`service/task_polling.go` PersistTaskResultToOBS）：校验 `isUnderRoot(NFSRoot, nfsPath)` → `mediastore.Persist`（带审计 metadata：user-id/task-id/model/platform）→ 结果 URL 存 `obs://` 占位符，序列化时 `ResolveResultURL` 实时换签名 URL（TTL 默认 168h）。

### 7.5 配置（系统设置 `media_storage.*`，`setting/system_setting/media_storage.go`）

`enabled / provider(obs) / endpoint / region / bucket / access_key_id(优先 OBS_AK env) / secret_access_key(优先 OBS_SK) / nfs_output_root(默认 /nfs-output，**必须 == gpustack lightx2v_output_root**) / ingest_nfs_path / ingest_upstream_url / upstream_url_allowed_hosts / signed_url_ttl_hours(168) / max_object_size_mb(200)`。启动时 `nfsinput.ProbeNFSInputs()` 探测 `<root>/inputs/` 写读删，失败 fatal。

### 7.6 新增引擎/任务类型的复制模式（5 步）

1. `validTaskTypes` 加值 + `constants.go` ModelList 加模型名；模型名不规律则补 `inferTaskType()` 规则。
2. 声明输入约束（需图/纯文本），照 TTS/S2V/SR/VACE 模式写 `materializeXXXInputs()`（含 Cleanup 回滚）。
3. `BuildRequestBody()` 路由到新 materialize；新路径类字段加进 `legacyInputKeys`；引擎专有参数走 metadata 自然透传。
4. `ParseTaskResult()` 一般无需改（状态集合是门面统一的）。
5. 配置侧：per-model 大小上限（AudioModelConfig / VideoModelConfig）按需登记。

短句类任务（TTS 5-8s）异步轮询略重，可选优化：引擎/门面补 `/v1/tasks/<kind>/sync` 同步变体（图片链路已有先例）。

---

## 8. P4 — 部署

### 8.1 Server（238）

```bash
docker pull <ACR>/gpustack:lx2v-<不可变tag>
docker stop gpustack-server && docker rm gpustack-server
docker run -d --name gpustack-server --restart unless-stopped -p 80:80 \
  -v gpustack-data:/var/lib/gpustack -v /nfs-output:/nfs-output \
  <镜像> --system-default-container-registry quay.io
docker logs gpustack-server | grep -E "Init built-in backend|migration"   # alembic 自动 upgrade ~0.9s
```

### 8.2 Worker（`lx2v-node.sh` 子命令全集）

| 命令 | 用途 | 关键参数 |
|---|---|---|
| `install` | 新节点接入（9 步 ~16min） | `--token` `--worker-ip` `--offline` `--clean-residue` `--force` |
| `upgrade-gpustack` | 升级 worker 镜像 | `--offline` |
| `upgrade-engine` | 升级引擎镜像 | `--engine {lightx2v\|indextts}` |
| `status` | 一屏巡检 worker/镜像/NFS/显存/实例 | — |
| `clean` | 清残留 | `--purge-data` `--kill-gpu-procs` |
| `prepare-transfer` | （238 专用）出包后 pull 三镜像 → save tar → NFS | — |

install 九步：驱动/架构预检(5s) → 残留扫描+IP 解析(5s) → apt docker.io/nfs-common(1.5min，逐个装) → **先写 fstab 再 mount -a**（`-t nfs -o vers=3,timeo=600,nolock,noresvport,_netdev`）+ 软链 → nvidia-container-toolkit(5-8min) → gpustack tar load(4.4G/1.5min) → lightx2v tar load(9.8G/4min) → indextts2 tar load(10G/4min) → 起 worker 验证 `Worker registered` + UI Ready。

批量（60 卡实录）：`lx2v-fleet.sh upgrade-gpustack`（并发 5）/ `-j 3 upgrade-engine`（大 tar 降并发防 NFS 抢）。

**新节点第一坑**：脚本全绿、注册成功但 UI 永不 Ready = server→worker **TCP 10150** 被安全组拦（`curl http://<worker>:10150/healthz` 超时确认），加入既有节点同款安全组。

### 8.3 模型部署（UI Models → Deploy Model）参数速查

| 模型 | Model Path | Backend | GPUs/Replica | Backend Parameters | 显存/耗时参考 |
|---|---|---|---|---|---|
| z-image (t2i) | Z-Image-Turbo | LightX2V | **1（显式）** | 无 | 20G，7.6s/张 |
| wan2.2-t2v | Wan-AI/Wan2.2-T2V-A14B | LightX2V | 4 | 无 | 35G/卡，133s(720p+RIFE) |
| wan2.2-i2v | 同 T2V 目录 | LightX2V | 4 | `--model-cls wan2.2_moe_distill` `--task i2v` | 33G/卡，68s |
| infinitetalk-480p | Wan2.1-I2V-14B-480P | LightX2V | 4 | `--model-cls infinitetalk` `--profile infinitetalk-480p/int8-4card` | 54s/5s |
| qwen-edit (i2i) | Qwen-Image-Edit-2511 | LightX2V | 1 | 无（路径含 Edit 自动选） | 20G+60G Shmem |
| seedvr2 (sr) | SeedVR2-3B | LightX2V | 1 | 无 | 27.5G |
| indextts-2 (tts) | IndexTTS-2 | IndexTTS | 1 | 无 | 8-10G 整卡预订，1-2min 加载 |

部署纪律：
- 单卡模型**必须显式 GPUs per Replica = 1**；同 (GPU数,task) 多 profile 变体必须 `--profile` 显式 pin。
- 部署前 `nvidia-smi` 清野进程；**错峰启动 ≥2min/实例**，每步等 `/ready` 200 + `MemAvailable ≥15G`。
- Shmem 大户（qwen/vace ~55-60G/实例，调度器只看显存看不见）单节点 ≤2 副本；看 `grep Shmem /proc/meminfo` 不看 free。
- 引擎 config 内路径与集群 NFS 不一致：宿主软链 `ln -sfn /nfs-models/<share> /nfs-data` + worker `-v /nfs-data:/nfs-data`（自动复制给推理容器）。
- UI 显示"N×40 GiB"整卡预订是 TTS 类正确表现。

---

## 9. P5 — 测试验证

### 9.1 发布流程

1. **补丁逐文件评审**（见 §3 补丁纪律）→ 3 commit 合入。
2. **出镜像**：引擎 CI 出双 tag → 238 `prepare-transfer` 更新 NFS tar；gpustack 改动走 overlay 出包。
3. **灰度**：一台节点新镜像部署基准模型，压测回归**对齐历史稳态数值**（如 InfiniteTalk int8-triton = 93.8s，偏差即回归）。
4. **金丝雀**：新旧并跑对样出片核画质。
5. **全量**：fleet 脚本滚动升级；config 变更（如 torchao→triton）随镜像同车发布。

### 9.2 部署后验收

```bash
# ① launcher 选型正确
docker logs $(docker ps --format '{{.Names}}' | grep <model> | grep run) 2>&1 | grep launcher | head -2
#    → model_cls=... gpus=N profile=... internal_port=... master_port=...
# ② 资源符合预期
nvidia-smi ; grep -E 'Shmem|MemAvailable' /proc/meminfo
# ③ 端到端冒烟
curl -X POST http://10.0.0.238/v1/videos -H "Authorization: Bearer <KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image","task_type":"t2i","prompt":"...","user_id":1}'
curl http://10.0.0.238/v1/videos/<task_id>                     # → done + nfs_path
curl http://10.0.0.238/v1/videos/<task_id>/content -o out.png  # 可下载
# ④ 并发分流：并发 4 发，验证 least-pending 落到 4 实例各 1
# ⑤（可选专项）kill 引擎容器 → 观察 sweeper requeue + 重派（-r1 新路径）
```

### 9.3 单元测试跑法（gpustack fork）

```bash
cd gpustack && uv run --no-sync pytest        # 2026-07-14 全绿：1238 passed, 12 skipped, ~2min
```

- **必须带 `--no-sync`**：venv 依赖已装齐，裸 `uv run pytest` 每次触发全量 depsync（拉 torch 等巨型依赖）导致超时——这就是之前"pytest 跑不通"的原因。
- **CI 门禁**：`pack-acr-overlay.yml` 已加 test job（build needs test），保证出的每个 overlay 镜像都过单测；pre-commit 保持秒级静态检查不挂 pytest（避免 --no-verify 绕过文化）。
- **macOS 开代理的机器**：httpx 经 `urllib.getproxies()` 会读 macOS 系统代理但忽略其 localhost 例外清单，回环 TLS 测试会被路由进本地代理而 ReadTimeout。`tests/utils/test_ssl_context.py` 已改为 `trust_env=False` 隔离；后续新增涉及本地回环 HTTP 的测试同样要加。

### 9.4 已知未专项验证项（接手人注意）

- 死亡重派链路：代码+设计完整，专项破坏性验证**暂缓**（用户决策 2026-07-14，不阻塞）。
- janitor 删除行为：需时间积累观察 TTL/水位实际动作。
- 引擎 api_server 无自动化测试（M1 交付手测，可在后续补集成测试）。
- IndexTTS 的 M5 UI 音频面板：**不做**——当前重心是引擎改造，后续可能不直接使用 IndexTTS；其价值保留在"第二引擎接入范式"（异步契约/镜像/门面 4 处小改的模板）。

---

## 10. 监控、计量与观测

- **/metrics**：launcher 代理引擎 Prometheus 端点（加载时长/推理耗时等）；VIDEO 品类在 `runtime_metrics_aggregator` 跳过 scrape 列表（无 vLLM 式 /metrics 的品类别乱 scrape）。
- **用量计费**：`OperationEnum.VIDEO_GENERATION` per-request 计数（无 token），进 GPUStack 用量报表。
- **latency-probe 日志**（补丁 P10 模式）：提交（带排队数）/轮询/**轮询 MISS 告警**（亲和错配证据）/取片速度 MB/s，纯日志零行为变更，用于时延归因（轮询间隔 vs 亲和错配 vs 大文件代理谁背锅），再决定要不要上回调推送。
- **管理面**：`GET /v2/video-tasks` 任务列表（admin/owner 作用域）+ UI video-tasks 面板（polling）+ Storage Settings 页。
- 节点巡检：`lx2v-node.sh status`。

**300 节点前瞻**（backend-design 已预埋，当前 60 卡不需要）：worker 本地轮询+心跳上报、中央 SKIP-LOCKED 调度循环、引擎完成 webhook 推送、每模型背压 429、热状态 Redis 缓存。

---

## 11. 升级与回滚

- **升级**：server 换不可变 tag 重拉容器（数据卷保留，迁移自动跑）；worker/引擎走 fleet 脚本；引擎镜像升级只影响新实例，滚动重建。
- **回滚**：镜像层面回退到上一个不可变 tag 即可；**数据库迁移设计为向后兼容**（加表/加枚举值，不改旧表），旧镜像跑新库无碍；overlay 的迁移链修复保证任何 base 版本上链头一致。
- 历史任务的 `output_root` 记在任务行，改配置/回滚不影响旧结果取件。
- 镜像 tag 纪律：生产只引用不可变 tag（日期+sha8），`latest`/`lx2v-dev` 仅用于开发。

---

## 12. ARM + A100 坑点全表

### 依赖/构建

| 坑 | 现象 | 解法 |
|---|---|---|
| PyPI aarch64 torch 只有 CPU 版 | CUDA 不可用 | FROM arm64 cu128 base；pyproject 删 cu128 索引、删 uv.lock；pip 约束冻结 torch |
| numpy 2.x 冲突 | numba/librosa 崩 | 约束只锁 torch，numpy 显式装回 1.26.2 |
| pynini/OpenFst 无 wheel | 编译失败 | 换 `wetext>=0.0.9` + 改 front.py Linux 分支 |
| decord 无 ARM wheel（空壳包） | VACE/Animate 必崩 | 源码编译（ffmpeg-dev+cmake）或 PyAV 回退补丁 |
| torchaudio 2.11 save() | 缺 torchcodec ModuleNotFoundError | 改 soundfile.write |
| torchvision ≥0.23 | read_video_timestamps 被移除 | PyAV 回退（P6） |
| BigVGAN CUDA kernel | 每启动 nvcc JIT 3-5min | `use_cuda_kernel=False`（vocoder 0.23s 输出一致） |

### 运行时

| 坑 | 现象 | 解法 |
|---|---|---|
| flash_attn3 | A100 直接崩（Hopper 专属） | torch_sdpa |
| sage_attn2 | Qwen 系黑图（NaN） | torch_sdpa |
| flashinfer rope | NoneType not callable | rope_type=torch |
| MoE bf16 多卡 | 每 rank 复制全量权重 CPU OOM | int8 only；--memory=240g |
| MoE offload block 粒度 | 换专家 buffer 不清 → 黑屏 | granularity=model |
| Shmem 大户 | 第 3 副本起来整机 OOM-kill | ≤2 副本/节点，看 /proc/meminfo Shmem |
| triton 首请求 | per-shape autotune 慢 | launcher warmup / 挂流量前预热一发 |
| GPU 野进程 | 新实例 OOM 但 nvidia-smi 算不上账 | 部署前清场 kill |
| NUMA | 双实例互抢内存带宽（~16% 膨胀） | 各绑一个 NUMA 域（GPU0,1@node0 / GPU2,3@node2） |

### NFS/存储/网络

| 坑 | 现象 | 解法 |
|---|---|---|
| 并发冷启 | 3×T5 并发读 NFS 打进 18min 病态 | 错峰 ≥2min，等 /ready + MemAvailable≥15G |
| page cache 挤压 | offload 权重驻留把 T5 cache 挤出 → 冷读 8min | offload 重模型避开慢节点；诊断 `dd if=/nfs-models/<file> of=/dev/null bs=1M count=1000` |
| fstab 顺序 | 重启挂载消失 | 先写 fstab 再 mount -a |
| docker save -o | NFS 上 Exit 1 + .tmp-* 残留 | 重定向 `>`；ls -la 清 .tmp-* |
| 多架构 save | x86 上 content digest not found | 先 pull --platform linux/arm64 |
| 直拉大镜像 | 29G 跨节点中途重置 | 238 中转：pull → save NFS → load |
| 安全组 | worker 注册但 UI 不 Ready | 放行 TCP 10150（同款安全组） |
| HF 下载 | Xet/镜像故障 | HF_HUB_DISABLE_XET=1 + hf-mirror；ModelScope 优先 + 完整性自检 |
| zsh | 行内 `#` 注释导致命令错 | 交互 shell 命令不带行内注释 |

---

## 13. 新引擎接入 Checklist

**P0 POC**
- [ ] harness 跑通，热态稳态矩阵（量化×卡数×分辨率），产物防呆三检（体积/熵/黑屏）
- [ ] 实验测试报告 + 生产推荐配置 + 判死结论
- [ ] A100 config：torch_sdpa / rope=torch / offload 与量化策略确定

**P0.5 引擎接模型**（LightX2V 内新增 model_cls 时）
- [ ] runner/weights/infer 按目录约定，无 batch 维，CFG 串行
- [ ] 权重离线转 x2v，config json 只放稳定 profile
- [ ] 与上游 pipeline 数值对齐 7 步验证
- [ ] 补丁逐文件评审记录（公共文件补丁证明零副作用）

**P1 异步化**
- [ ] 5 端点 + /ready；状态字符串与 `_ENGINE_STATE_MAP` 逐字核对（cancelled 双 L）
- [ ] FIFO：max_queue 可配、503 背压、stop_event 取消、PENDING→PROCESSING 原子
- [ ] 输入 NFS 绝对路径字段、输出 `.part{ext}` 原子写、重启丢任务（不持久化）
- [ ] warmup 完成前 /ready 503

**P2 镜像**
- [ ] Dockerfile FROM arm64 base + torch 约束冻结 + 训练依赖剔除 + ARM 替代包
- [ ] launcher + profiles.yaml 进镜像（config_json 指镜像内路径；warmup 块按需）
- [ ] CI：arm 原生 runner、ACR base 已种、双 tag、构建断言
- [ ] 权重 tar.gz + hf_cache 预填 → NFS；HF_HUB_OFFLINE 下启动验证

**P3 GPUStack**
- [ ] BackendEnum + get_built_in_backend（custom_framework="cuda" + 显式 image_name + /ready）
- [ ] worker/backends/<engine>.py（挂载只读 GPUSTACK_EXTRA_MOUNTS）+ serve_manager 映射
- [ ] selector（profile 固定卡数）+ scheduler elif
- [ ] 门面 4 处：task_type 白名单 / _engine_kind / _output_ext / _model_latency
- [ ] janitor 输入保护键覆盖新 `*_path` 字段；model-catalog + 图标；UI 4 处枚举
- [ ] **Dockerfile.acr COPY 清单加新文件** + import 断言 + 迁移（如有新表）
- [ ] overlay 出包双架构，构建断言全绿

**P3.5 new-api**
- [ ] `validTaskTypes` + ModelList + `inferTaskType()` 规则
- [ ] `materializeXXXInputs()`（Cleanup 回滚 + magic bytes + per-model 大小上限）
- [ ] 新路径类字段加进 `legacyInputKeys`；引擎专有参数确认能透传（不在剥离清单）
- [ ] NFSRoot == lightx2v_output_root，双侧启动探测

**P4 部署**
- [ ] server 换 tag（看迁移+backend 注册日志）；prepare-transfer 更新 NFS tar；fleet 升级
- [ ] UI 部署：GPUs/Replica 显式、--profile pin、错峰、清场、Shmem 红线
- [ ] 新节点安全组核查（TCP 10150）

**P5 验证**
- [ ] 灰度回归稳态数值；金丝雀对样
- [ ] 冒烟：提交/轮询/content + 并发 least-pending 分流
- [ ] （建议补）kill 实例验证死亡重派；janitor 观察期
- [ ] 文档沉淀：实验报告 + 部署实录 + 运维手册 + 交接文档 + 本方法论更新

---

## 附录 A：关键文件与文档索引

**gpustack fork**
| 内容 | 位置 |
|---|---|
| backend 定义 | `gpustack/schemas/inference_backend.py:370-453` |
| 门面（契约/校验/状态映射） | `gpustack/routes/videos.py`（解析 489-534，校验 292-346，STATE_MAP 178-184，content 1098-1122） |
| worker backend | `gpustack/worker/backends/{lightx2v,indextts}.py` |
| selector | `gpustack/policies/candidate_selectors/{lightx2v,indextts}_resource_fit_selector.py` |
| sweeper / janitor | `gpustack/server/video_task_sweeper.py` / `video_storage_janitor.py` |
| overlay 出包 | `pack/Dockerfile.acr`（COPY 29-65，迁移修复 77-86，断言 87-106） |
| 设计/计划/交接 | `docs/lightx2v-backend-design.md` / `-builtin-backend-plan.md` / `-m4-m5-handover.md` |
| NFS 输入设计 | `docs/lightx2v-nfs-input-design.md` |
| 部署实录/运维 | `docs/lightx2v-gpustack-部署实录.md` / `-节点运维手册.md` / `-60卡集群部署实录-2026-07-12.md` / `-20260706-发布部署验证全记录.md` |
| 二次开发流水线 | `docs/secondary-development-pipeline.md` |

**LightX2V**
| 内容 | 位置 |
|---|---|
| launcher | `deploy/gpustack-lx2v-launcher/gpustack_lx2v_launcher.py` + `profiles.yaml` |
| 引擎 API schema | `lightx2v/server/schema.py:75-93` |
| ARM Dockerfile | `dockerfiles/Dockerfile_aarch64_cu128` / `Dockerfile_aarch64_app` |
| 工程脚本 | `scripts/{download_models.sh, convert_int8.sh}` / `scripts/smoke/{test_model.sh, smoke_test_a100.sh, run_batch.sh}` |
| 新模型接入 skill | `.claude/skills/support_new_model/SKILL.md` |
| 出包/部署/测试文档 | `docs/{出包清单-补丁评审.md, 现网部署清单.md, 视频模型测试-交接文档.md}` |
| 平台选型 | `docs/{视频生成平台-轻量自建方案设计.md, sub2api_vs_new-api_对比分析.md}` |

**new-api**
| 内容 | 位置 |
|---|---|
| 异步 TaskAdaptor | `relay/channel/task/gpustackplus/adaptor.go`（白名单 59-62，剥离 67-74，BuildRequestBody 152-290，TTS 物化 532-558，状态映射 622-640） |
| 输入物化工具包 | `relay/channel/gpustackplus/nfsinput/nfsinput.go` |
| 同步图片链路 | `relay/channel/gpustackplus/adaptor.go` |
| OBS 落盘/签名 | `service/task_polling.go` / `service/media_ingest.go` / `service/mediastore/` |
| 媒体存储配置 | `setting/system_setting/media_storage.go` |
| per-model 上限 | `common/media_model_config.go` |
| OBS 设计文档 | `docs/media-storage-obs-design.md`（v1.4） |
| 关键提交 | b74041862（OBS+渠道）→ 94a913447（对齐 M4 门面）→ 13844d618（TTS）→ abf2233cc（s2v/sr/vace） |

**index-tts（第二引擎范式）**
| 内容 | 位置 |
|---|---|
| 异步 API | `indextts/api_server/{task_manager,routes,schemas,worker}.py` + `server.py` |
| 镜像 | `Dockerfile.lightx2v`（36 行）+ `.github/workflows/build-arm64.yml` |
| 改造方案/踩坑 | `docs/{indextts2-异步内嵌-改动方案.md, indextts2-arm64-集成与踩坑记录.md}` |
| 关键提交 | 94cfb32（arm64 镜像+CI）→ 39ca756（M1 异步 API）→ 353c9de（.part 扩展名修复） |
