# ACE-Step-1.5 内嵌 GPUStack 后端 — 设计文档

> 第三个内嵌引擎(继 LightX2V 视频/图像、IndexTTS-2 语音之后)。本文遵循
> [`docs/新引擎内嵌gpustack-工程化方法论.md`](新引擎内嵌gpustack-工程化方法论.md) 的五阶段/M1–M5 模板,
> 只记录 ACE-Step 特有的落点与坑。范式对照见 [`lightx2v-backend-design.md`](lightx2v-backend-design.md)、
> IndexTTS 范式见 `index-tts/docs/indextts2-arm64-集成与踩坑记录.md`。
>
> 硬件基线:鲲鹏 ARM (aarch64) + 4×A100 40G PCIE / 256G RAM,x86 manager (10.0.0.238),华为云 SFS(NFS)。
>
> **范围决策(2026-07-17,用户拍板)**:
> - Phase 1 上 **text2music + cover + repaint** 三种任务(extract/lego/complete 仅 base 模型,后置)。
> - GPUStack 里 **新增独立 `CategoryEnum.MUSIC` 品类**(不复用 text_to_speech),接受 ~38 个 category gate 点的扫描成本。

---

## 0. 一句话形态

把 ACE-Step-1.5(文生音乐/歌曲,DiT + VAE + 5Hz-LM 三件套)接成 GPUStack 内置**异步有状态**后端 `BackendEnum.ACESTEP`,复用 lightx2v 的门面(`/v1/videos`)/sweeper/janitor 全套组件,终端经 new-api `gpustackplus` 渠道提交 → 产物落 NFS → OBS 取件。

---

## 1. ACE-Step 与前两引擎的三个结构性差异

| 维度 | LightX2V | IndexTTS-2 | **ACE-Step-1.5** |
|---|---|---|---|
| 品类 | video/image | text_to_speech | **music(新增)** |
| 已有异步框架 | 需自建 | 需自建 | **✅ 自带**(`acestep/api_server.py` + `acestep/api/`,内存队列 + `_JobStore` + worker,`workers=1`) |
| 模型件数 | DiT(+VAE) | 单模型 | **DiT + VAE + 5Hz-LM(4B via nano-vllm)三件套,两个模型路径** |
| 输入 | 文本/图/音/视频 | 文本 + 参考音色 | 文本(t2m)/ 参考音频(cover)/ 源音频+区域(repaint) |
| 输出 | mp4/png | wav | **mp3(默认)/wav/flac/opus/aac** |
| 卡数 | 1/4 | 1 整卡 | 待 P0 定(倾向 1 整卡) |

**要点**:ACE-Step 自带异步任务框架是最大红利 —— M1 不重写队列,只加一层**门面契约适配路由**;但它的原生契约(`/release_task` + `/query_result` 批量轮询、状态字 `queued/running/succeeded/failed`、`{data,code,error}` 信封、`/health`)**与门面 `_ENGINE_STATE_MAP` 不一致**,必须适配(方法论 §4.1 铁律:对齐引擎到门面,不反过来)。

---

## 2. ACE-Step 原生 API 契约速查(适配层的输入侧)

启动:`ACESTEP_MODE=api` → `uvicorn acestep.api_server:app`(内存队列,强制 `workers=1`);启动期 lifespan 加载 DiT + LM。

| 端点 | 形态 |
|---|---|
| `POST /release_task` | JSON 或 multipart;返回 `{code,data:{task_id, status:"queued", queue_position}}`;队列满 429 |
| `POST /query_result` | body `{task_id_list:"[id,...]"}`(**批量**);返回 `{code,data:[{task_id, result:"<json字符串>", status(0/1/2), progress_text}]}` |
| `GET /v1/audio?path=<urlencoded 绝对路径>` | 音频字节流(产物下载) |
| `GET /health` | 进程存活 |
| `GET /v1/models` | 模型列表 |

- **状态映射**(`server_utils.STATUS_MAP`):`queued→0, running→0, succeeded→1, failed→2`(**没有 pending/processing/cancelled 语义**)。
- **产物**:成功后 `store` record `result.audio_paths` = `["/v1/audio?path=<abs>", ...]`;`query_result` 的 `result` json 列表项含 `file`(该 URL)+ `metas`。
- **请求体核心字段**(`GenerateMusicRequest`,73 字段,取子集):
  - 通用:`prompt`(=caption 描述)、`lyrics`、`thinking`(true=用 LM 生成音频码)、`model`、`bpm`、`key_scale`、`vocal_language`、`time_signature`、`audio_duration`、`inference_steps`(默认 8)、`guidance_scale`(7.0)、`seed`/`use_random_seed`、`audio_format`(默认 **mp3**)、`task_type`、`lm_model_path`、`lm_backend`(**vllm/pt/mlx**)。
  - cover:`reference_audio_path`、`audio_cover_strength`、`cover_noise_strength`。
  - repaint:`src_audio_path`、`repainting_start`/`repainting_end`、`repaint_mode`(conservative/balanced/aggressive)、`repaint_strength`。

**arm A100 关键**:`lm_backend=pt`(纯 PyTorch)可绕开 nano-vllm + flash-attn(aarch64 无 wheel);Dockerfile 默认 `ACESTEP_LLM_BACKEND=pt`。这是 P0 判死项之一。

---

## 3. M1 — 引擎异步适配层(ACE-Step 仓,核心工作量)

新增 `acestep/api/http/tasks_facade_routes.py`(照 ACE-Step 的 AGENTS.md:模块 ≤200 LOC、docstring 强制、配 unittest),**复用现有 `app.state.job_queue` / `_JobStore` / worker**,只做协议翻译:

### 3.1 端点(与门面 `_ENGINE_STATE_MAP` 逐字对齐)

```
POST   /v1/tasks/music/           → 200 {task_id, task_status, save_result_path} | 503 队列满 | 400 缺参
       (别名 /v1/tasks/audio/;门面按 engine_kind "music" 发 /v1/tasks/music/)
GET    /v1/tasks/{task_id}/status → 200 {task_id, status, save_result_path, error, error_type,
                                          created_at, completed_at} | 404(触发门面死亡重派)
GET    /v1/tasks/queue/status     → {is_processing, current_task, pending_count, active_count,
                                     queue_size, queue_available}
DELETE /v1/tasks/{task_id}        → 取消
GET    /ready                     → 503 加载/warmup 中 | 200 就绪(GPUStack health_check_path)
GET    /health                    → 复用现有(launcher/探活)
```

### 3.2 状态映射(store → 门面)

| ACE-Step store | 门面 status | 门面映射 |
|---|---|---|
| `queued` | `pending` | ASSIGNED |
| `running` | `processing` | RUNNING |
| `succeeded` | `completed` | DONE |
| `failed` | `failed` | FAILED |
| (取消) | `cancelled` | CANCELED(**双 L**) |

> ACE-Step 无原生 cancelled,DELETE 后适配层自记 `cancelled` 终态即可。

### 3.3 输出落 NFS(方法论 §4.3 血泪点)

- 适配层接受门面注入的 `save_result_path`(NFS 绝对路径,含扩展名)→ 从扩展名反推 `audio_format`(`.mp3→mp3`)传给引擎。
- 引擎写自己的 output dir 后,适配层把产物 `.part<ext>` 原子写到注入路径 → `os.replace()`(soundfile/ffmpeg 按后缀推编码,扩展名必须保留)。
- 目录结构遵守 janitor 约定:`<root>/<task_type>-<model>/YYYY/MM/DD/<user_id>/<task_id>.<ext>`。

### 3.4 明确不做

不持久化任务(重启丢任务是特性,靠门面 sweeper 重派)、不鉴权、不清理输入(janitor 负责)。可保留原生 `/release_task` 作手测兼容。

---

## 4. M2/M3 — GPUStack 内嵌(gpustack fork)

### 4.1 新品类扫点(本次比 IndexTTS 多出来的成本)

新增 `CategoryEnum.MUSIC = "music"` 后,逐条 grep ~38 个 category/backend gate 点(照 lightx2v 首接 VIDEO 清单):
- `scheduler/evaluator.py`:MUSIC 跳 runtime-version 检查(同 VIDEO 臂)。
- `worker/runtime_metrics_aggregator.py`:MUSIC 跳 `/metrics` scrape(无 vLLM 式 metrics)。
- `worker/model_meta.py`:类型识别(ACE-Step 权重 → MUSIC)。
- `schemas/models.py`:`BackendEnum.ACESTEP = "ACEStep"`。
- UI 类目枚举(gpustack-ui 4 处)。
- **逐个 grep 现有 `CategoryEnum.VIDEO` / `IMAGE` / `TEXT_TO_SPEECH` 的使用点,确认 MUSIC 是否需要同臂处理**。

### 4.2 backend 注册 & worker

| 文件 | 改动 |
|---|---|
| `schemas/inference_backend.py` | `get_built_in_backend()` 加一条:`custom_framework="cuda"` + 显式 ACR 镜像名 + `health_check_path="/ready"` + `parameter_format=SPACE` |
| `worker/backends/acestep.py`(新建) | 继承 `InferenceServer`,对称 `indextts.py`;注入两个模型路径(`ACESTEP_CONFIG_PATH` + `ACESTEP_LM_MODEL_PATH`)、`ACESTEP_MODE=api`、`ACESTEP_LLM_BACKEND=pt`、离线 flag;只读 `GPUSTACK_EXTRA_MOUNTS` |
| `worker/serve_manager.py` | `_SERVER_CLASS_MAPPING` 加一条 |
| `policies/candidate_selectors/acestep_resource_fit_selector.py`(新建) | 继承 lightx2v 版,固定卡数由 P0 定(倾向 1 整卡预订,同 IndexTTS) |
| `scheduler/scheduler.py` | 一条 elif + selector_map 一条 |

启动命令模板:`python3 -m uvicorn acestep.api_server:app --host {{worker_ip}} --port {{port}}`(或经 launcher,若需 warmup 预热 triton)。

### 4.3 门面 4 处(`routes/videos.py`)

1. `_MUSIC_TASK_TYPES = {"t2m", "cover", "repaint"}` 并入 `_VALID_TASK_TYPES`。
2. `_engine_kind()` 加 music 分支 → 提交到 `/v1/tasks/music/`。
3. `_output_ext()` → `.mp3`(music 默认;适配层据此设 audio_format)。
4. `_model_latency()` 加 ACE-Step 默认时延(P0 实测填,t2m 约数十秒~分钟级)。
5. janitor 输入保护键覆盖 `reference_audio_path` / `src_audio_path`。
6. `input_refs` 六层校验自动继承(cover/repaint 的音频输入字段:`reference_audio` / `src_audio`)。

### 4.4 出包

`pack/Dockerfile.acr` overlay COPY 清单**加入所有新文件**(backend/selector/schemas/门面/迁移)—— copy-list 漏文件是踩过的坑;import 断言加 `BackendEnum.ACESTEP` + `CategoryEnum.MUSIC`;有新迁移则进迁移链修复。

---

## 5. M4 — new-api 对接(gpustackplus 渠道)

1. `validTaskTypes` 加 `t2m` / `cover` / `repaint`;`inferTaskType()` 规则:`acestep→t2m`。
2. `materializeMusicInputs()`(照 s2v 音频物化):cover 需 `reference_audio`,repaint 需 `src_audio`;magic-bytes 校验(WAV/MP3/FLAC/...)+ per-model 大小上限 + Cleanup 回滚。t2m 纯文本零物化。
3. 新路径字段(`reference_audio_path` / `src_audio_path`)加进 `legacyInputKeys`(强制剥离,由门面 dictates);引擎专有参数(bpm/lyrics/repaint_mode 等)走 metadata 自然透传。
4. `ParseTaskResult()` 一般无需改(状态集合门面统一);per-model 音频大小上限登记。

---

## 6. P0 POC 测试计划(**动工前必做,判死项**)

真机(节点)跑 `ACE-Step-1.5/scripts/smoke/smoke_acestep_a100.sh`(见该脚本),按方法论 §2.1 harness:起容器 → 等 `/health`(记加载时长)→ 提交 → 5s 轮询 → 采样显存(**多卡取最大值**)/GPU 利用率/Shmem/MemAvailable → **只认 succeeded** → 产物防呆。

### 6.1 测试矩阵

| 用例 | task_type | 输入 | 变量 |
|---|---|---|---|
| A t2m-turbo | text2music | 纯文本(caption+lyrics) | LM 4B / 1.7B / 0.6B;duration 30/60/160s;thinking on/off |
| B t2m-instrumental | text2music | caption + `[inst]` | — |
| C cover | cover | reference_audio(参考风格) | audio_cover_strength |
| D repaint | repaint | src_audio + 区域 | repaint_mode balanced/aggressive |
| E lm_backend | text2music | 同 A | **pt vs vllm**(arm 判死项) |

### 6.2 采样与判死

- **显存/卡数**:DiT + VAE + LM(4B) 单卡 40G 是否够 → 定 selector 固定卡数;不够降 LM 到 1.7B/0.6B。
- **lm_backend=pt 能否在 arm A100 跑通**(绕 nano-vllm/flash-attn);vllm 后端能否退 torch_sdpa/eager。
- 热态稳态:连发 6 条丢首张取均值(冷态含 CUDA context + triton autotune 虚高)。
- 安静宿主:`docker rm -f` 清场后测;容器 `--memory=240g`。

### 6.3 产物防呆(音频)

- 产物 <20KB 判失败(防空文件假绿)。
- `ffprobe` 读时长,与请求 `duration` 偏差过大告警。
- `ffmpeg silencedetect`:整段静音判失败(防黑屏等价物)。

### 6.4 实验报告产出

环境(GPU/镜像 tag/起容器命令)→ 模型路径(DiT config + LM)→ A100 config(注明 lm_backend=pt 等必改项)→ 速度/显存矩阵(标注热态,多卡取最大)→ 关键发现(瓶颈归因)→ 部署建议(推荐 LM 档位 + 卡数 + 密度上限)→ 坑点。落到 `ACE-Step-1.5/docs/acestep-a100-poc实验报告.md`。

---

## 7. P2 — 镜像/依赖风险(P0 通过后)

| 风险 | 说明 | 应对 |
|---|---|---|
| **cu130 vs cu128 base** | ACE-Step `requirements.txt` aarch64 钉 `torch 2.10.0+cu130`,现有 lightx2v/indextts base 是 cu128 | 先试**复用 lightx2v arm64 cu128 base** + torch 约束冻结能否跑(能则秒级增量出包);不行再起 cu130 arm64 base 并在 ACR 种 base |
| **nano-vllm + flash-attn** | LM 推理依赖,aarch64 无 flash-attn wheel | `lm_backend=pt` 规避;确认 pt 后端不 import nano-vllm/flash-attn |
| **torchcodec aarch64** | `requirements.txt` 已标 `torchcodec ... platform_machine != 'aarch64'`,arm 不装 | 存 wav/mp3 走 soundfile/ffmpeg,别用 torchcodec 后端(同 IndexTTS 坑 #5) |
| **gradio 6.2.0 + 训练重依赖** | lightning/tensorboard/peft 训练用 | 构建时剔除训练依赖(方法论 §5.1 三原则) |
| **两个模型路径** | DiT config + LM checkpoints,都挂 NFS | 联网机预填 → NFS RO 挂载;`HF_HUB_OFFLINE=1` 离线加载 |

CI:原生 `ubuntu-24.04-arm` runner、ACR 一次性种 base、双 tag(`acestep:arm64-a100-latest` + 时间戳-sha8)。照 `index-tts/.github/workflows/build-arm64.yml`。

---

## 8. 里程碑与验收

| M | 仓 | 验收 |
|---|---|---|
| **P0** | ACE-Step | smoke harness 全绿:t2m/cover/repaint 出可听音频,显存/卡数/lm_backend 结论,实验报告 |
| **M1** | ACE-Step | curl 全链路:`POST /v1/tasks/music/` → 轮询 status → 产物落 NFS;`/ready` 加载前 503;unittest 覆盖状态映射 + 原子写 |
| **M2/M3** | gpustack | 容器拉起、`/ready` 200、门面接收 `t2m/cover/repaint` 分发正确;category 扫点无遗漏;`uv run --no-sync pytest` 全绿 |
| **M4** | new-api | 端到端:new-api submit → 物化(cover/repaint)→ 推理 → OBS 取件 |
| **M5** | gpustack-ui | music 类目 + 提交/试听/下载(可后置) |

顺序:P0 → M1 → M2/M3 并行 → M4 → M5。

---

## 9. 附录:关键文件索引

**ACE-Step-1.5**
| 内容 | 位置 |
|---|---|
| 现有异步 server | `acestep/api_server.py` + `acestep/api/`(route_setup / jobs/store / http/*) |
| 请求模型 | `acestep/api/http/release_task_models.py`(`GenerateMusicRequest`) |
| 状态映射 | `acestep/api/server_utils.py`(`STATUS_MAP`) |
| 产物 payload | `acestep/api/http/query_result_service.py` / `job_result_payload.py` |
| 任务类型/常量 | `acestep/constants.py`(`TASK_TYPES` / `TASK_INSTRUCTIONS`) |
| 推理入口 | `acestep/inference.py`(`generate_music`)、`acestep/handler.py`(`AceStepHandler`) |
| M1 适配层(新建) | `acestep/api/http/tasks_facade_routes.py` |
| P0 harness(新建) | `scripts/smoke/smoke_acestep_a100.sh` |
| Dockerfile 现状 | `Dockerfile`(x86 cu128,ACESTEP_MODE api/gradio) |

**gpustack fork**:见方法论附录 A(backend / 门面 / selector / sweeper / janitor / overlay 出包)。
**new-api**:见方法论 §7(gpustackplus 渠道 / nfsinput 物化)。

---

*本文为设计基线,随 P0/M1 落地滚动更新。改动遵守仓规矩:ACE-Step 侧模块 ≤200 LOC + unittest;所有代码/脚本改动先检视 + 用户确认才 commit。*
