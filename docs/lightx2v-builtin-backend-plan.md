# LightX2V 内置一等公民后端 + 生产级 dispatcher — 落地计划

> 本文是 [`lightx2v-backend-design.md`](./lightx2v-backend-design.md) 的**实现计划配套**。设计文档 §13 曾判「内置后端化取消」;本计划在「LightX2V 为主力引擎」的新前提下**推翻该结论**,直接做内置一等公民,详见下方 Context。
> 状态:已评审通过,实施中(M1 进行中)。

## Context（为什么做）

LightX2V 是团队后续**主力视频/图像生成引擎**,决定把它从「Custom 后端手配」升级为 **GPUStack 内置一等公民后端**。这**推翻了设计文档 §13「内置后端化取消(2026-07-02)」的结论**——§13 的唯一依据是「当前收益接近零(边角 add-on)」,而主力引擎前提下收益变成「核心产品面的完整调度/界面/参数体验」,内置化立得住。

**目标**:界面 day-one 原生正确(VIDEO 类目 + 参数表单 + 体验区),**零 hack、零临时 UI、零返工**;生产直接建在内置架构上,**跳过 Custom 过渡**。

**已锁定的架构决策**:
1. 新增 `BackendEnum.LIGHTX2V` + `CategoryEnum.VIDEO`。
2. **镜像自包含**:`InferenceBackend` 行直接带 `image_name`/`version_configs`/`health_check_path=/ready`/`common_parameters`,**不碰兄弟仓 gpustack-runner**;换引擎镜像走改 InferenceBackend 行、不重打 GPUStack。镜像解析走 `base.py:_resolve_image` 第 2 条(`inference_backend.get_image_name` 读 version_configs),不依赖 runner。
3. **launcher shell-out**:`lightx2v.py` 只负责起 `gpustack-lx2v-launcher`,**profile 选择 / torchrun 拼接 / `/ready` 门控全在引擎镜像**;调 profile = 换引擎镜像,GPUStack 不知情。
4. selector **纯 profile 表驱动**(z_image=1卡、wan int8=4卡),不做显存搜索(同构节点 + 固定 profile,自动放置无用武之地)。
5. 对外 **OpenAI 兼容门面**:`/v1/videos`(作业式,可轮询)+ `/v1/images/generations`(同步)。
6. 多实例靠**中央 PostgreSQL 队列 + `central_id→实例` 亲和映射**(LightX2V task_id 是进程内内存态,轮询必须回原实例)。
7. **GPUStack 只写 NFS、状态返回 `nfs_path`**;OBS 全在 new-api(已生产上线)。

---

## 关键复用件（探索已确认）

| 用途 | 复用 | 路径 |
|---|---|---|
| 内置后端登记 | `get_built_in_backend()` + 开机播种 | `schemas/inference_backend.py:370` · `server/controllers.py:1517 _init_built_in_backends` |
| 后端模块基类 | `InferenceServer` ABC(`start()`/`_build_command_args()`/`_create_workload()`) | `worker/backends/base.py` · 参照 `vox_box.py`(最简) |
| 镜像解析(显式优先) | `_resolve_image()` 第 2 条 `inference_backend.get_image_name()` | `worker/backends/base.py:885` · `schemas/inference_backend.py:156` |
| 额外可写挂载 | `_get_configured_mounts()` + Ray sidecar 先例 | `base.py:566` · `vllm.py:281 ContainerMount` |
| server 类映射 | `_SERVER_CLASS_MAPPING` | `worker/serve_manager.py:76-81` |
| 最简 selector 模板 | `CustomBackendResourceFitSelector`(不做显存搜索) | `policies/candidate_selectors/custom_backend_resource_fit_selector.py` |
| selector 派发 | if/elif 链 + dict 映射 | `scheduler/scheduler.py:~442-465` · `~847` |
| 唯一成熟的实例转发路径 | `proxy_request_by_model()` | `routes/openai.py` |
| OpenAI 端点前缀 | `openai_model_prefixes`(`/images/generations` 已在、`/videos` 缺) | `gateway/utils.py:123-146` |
| 异步作业状态机先例 | `BenchmarkStateEnum` + model_files 轮询模式 | `schemas/benchmark.py:28` · `routes/model_files.py` |
| Alembic 迁移先例 | `add_env` batch_alter | `migrations/versions/2025_02_19_1743-*_add_env.py` · 生成用 `hack/generate-migration-revision.sh` |

---

## 里程碑（全部落在内置架构,无一次性代码）

### M1 — 枚举 + 内置登记 + 后端模块（「认得 + 起得来」层）

- **`schemas/models.py`**:`BackendEnum` 加 `LIGHTX2V = "LightX2V"`;`CategoryEnum` 加 `VIDEO = "video"`。✅ 已完成
- **`schemas/inference_backend.py:370 get_built_in_backend()`**:加 LIGHTX2V 行,**显式填** `image_name`/`version_configs`/`default_run_command`(`gpustack-lx2v-launcher --model {{model_path}} --port {{port}} --host {{worker_ip}}`)/`health_check_path="/ready"`/`common_parameters`(驱动 UI 参数表单)/`parameter_format`/`default_env`。
- **`server/controllers.py:1517`**:`_init_built_in_backends` 首次 `create` 会把 `get_built_in_backend()` 设的全部字段落库;`:1520` 对 `CUSTOM` 有特判,LIGHTX2V 走普通内置分支即可。
- **`worker/backends/lightx2v.py`(新建)**:继承 `InferenceServer`;`_build_command_args` **shell out 到 `gpustack-lx2v-launcher`**(经 `build_versioned_command_args`/`default_run_command` 展开占位符,不在 Python 里拼 torchrun/profile);`_create_workload` 追加 `ContainerMount(path="/nfs-rw")`(从 env/config 读开关,照 `vllm.py:281`);健康走 `/ready`。
- **`worker/serve_manager.py:76-81`**:`_SERVER_CLASS_MAPPING` 加 `BackendEnum.LIGHTX2V: LightX2VServer` + import。

### M2 — 调度接线（profile 表驱动 selector + 派发）

- **`policies/candidate_selectors/lightx2v_resource_fit_selector.py`(新建)**:照抄 `CustomBackendResourceFitSelector` 骨架;`gpus_per_replica` **查 profile 表定死**(不调 `estimate_model_vram()`);`_should_check_vision_tp_divisibility()→False`。
- **`policies/candidate_selectors/__init__.py`**:export。
- **`scheduler/scheduler.py`**:selector 派发 if/elif 链(~:460)加 `elif model.backend == BackendEnum.LIGHTX2V:` 分支;`selector_map` dict(~:848)加 `BackendEnum.LIGHTX2V.value: LightX2VResourceFitSelector`;如需要,`gpus_per_replica` 计算(~:836)加臂。
- `is_built_in_backend()` 门控(`scheduler.py:780`、`evaluator.py:370`)因 M1 加了登记**自动生效,无需改**。

### M3 — VIDEO 类目行为门控（分支审计）

审计约 38 个 gate 点(探索已出清单),**必改子集**:
- **`scheduler/model_registry.py detect_model_type()`**:加 VIDEO 识别(或部署期显式指定 categories,绕过自动探测)。
- **`scheduler/scheduler.py`** 自动分类链:VIDEO 臂。
- **`worker/serve_manager.py`** health-check `skip_categories`:加 VIDEO(视频走 `/ready` 而非 `/v1/models`)。
- **`schemas/models.py`** `is_llm`(:824)/`OMNI_CATEGORIES`(:840):确保 VIDEO 不被默认当 LLM。
- 其余（vLLM/SGLang/GGUF/Ascend 专属分支)**多数可忽略**,按清单逐条 grep 确认。

### M4 — OpenAI 门面 + 生产级 dispatcher（中央队列 + 多实例）

- **`gateway/utils.py:123-146`**:`openai_model_prefixes` 加 `/videos`。
- **`routes/videos.py`(新建)**:`POST /v1/videos`(收输入字节落 NFS→入队→返 job id)· `GET /v1/videos/{id}`(轮询,带 `nfs_path`)· `GET /v1/videos/{id}/content`(从 NFS 流式);图片走现有 `/v1/images/generations` 同步。
- **`routes/routes.py:~414`**:`include_router(videos.router, prefix="/v1")`。
- **`schemas/video_generation_task.py`(新建 SQLModel)+ Alembic 迁移**:中央队列表,`central_id→(实例, 原生 task_id, nfs_path)` 映射;状态机仿 `BenchmarkStateEnum`;`SELECT ... FOR UPDATE SKIP LOCKED` 取任务。
- **dispatcher**(server 侧,APScheduler):调度循环(空闲实例→出队→async submit→轮询感知完成,completion-source A ~2s)· 每模型限流(超阈值 429 + Retry-After)· 提交即参数校验(帧数 4n+1、时长上限、aspect_ratio 转置 bug 绕过)· 失败处理(任务级 failed 重试 vs 实例级死亡回 queued 重派)。复用 `get_running_instances`、`http_proxy/load_balancer.py`、`proxy_request_by_model`。
- **NFS Janitor**(APScheduler):按天分区 TTL(7天)+ 最小年龄保护(<1h 不删)+ 水位驱逐(85%→70%)。
- **部署要求**:GPUStack server 节点 + worker 节点挂 `/nfs-rw`(fstab/autofs,与代码无关)。

### M5 — UI（gpustack-ui,独立仓）

- 取消注释 Video 体验区(`config/routes.ts:81-91`,调 `/v1/videos`)。
- 原生 video 类目徽标;参数表单由 M1 的 `common_parameters` 驱动。
- 任务面板(仿 `resources/components/workers.tsx` `useTableFetch` + `watch:true`);存储设置页(仅 NFS)。

---

## 验证

1. **launcher 单测(脱离 GPUStack)**:`NVIDIA_VISIBLE_DEVICES` 设 0 / 0,1,2,3 断言生成命令符合 profile;`curl` 提交 + 取结果。
2. **内置识别 + UI**:`uv run gpustack start --gateway-mode disabled`;部署 LIGHTX2V z_image;**确认模型列表原生显示 video 类目 + 参数表单渲染正确**;`nvidia-smi` 核对卡数。
3. **多实例亲和**:并发提交 → 轮询(靠 `central_id` 找回原实例,不串)→ `/content` 从 NFS 流式下载;超阈值 429;杀实例 → 在飞任务回 queued 重派。
4. **参数校验**:帧数非 4n+1 / 超时长上限 → 提交 400 早拒。
5. `make lint && make test`。

---

## 风险 / rebase 高危点

- **`scheduler.py` selector 派发(~:442-465, ~:847)**:上游 churn 最猛的文件,LIGHTX2V 臂保持最小。
- **~38 个 category/backend gate 点**:按清单审计,多数可忽略,漏改会导致调度走错分支。
- **内置镜像解析冲突(已核,可控)**:`list_backend_configs`(`inference_backend.py:427`)对内置后端是在已有显式版本上 **append** runner 版本(`:443 versions.append` + `:452 dedupe`),LIGHTX2V 无 runner 条目 → append 空、显式版本存活,不会被覆盖;部署期镜像走 `_resolve_image` 第 2 条显式路径。
- **profile 表位置**:必须留引擎镜像 YAML,不硬编进 `lightx2v.py`,否则破坏「换镜像不重打 GPUStack」。
