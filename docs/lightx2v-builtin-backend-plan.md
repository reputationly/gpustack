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

### M4 — 薄门面 + 亲和映射 + 死亡重派（**已收敛**,2026-07-06）

> **收敛决策(实测三仓队列后定,见设计文档 §6.0)**:重 dispatcher（中央 SKIP-LOCKED 主调度循环 / 集群级限流 / 提交参数校验）是**300 节点前瞻,当前规模伪需求**,砍掉。真正要做的收敛成「**薄门面 + least-pending 选实例 + 亲和映射 + 死亡重派**」。依据:引擎每实例已有 FIFO 队列(max 10,满返 **503**)、串行处理;GPUStack 网关已按模型 round-robin 到 RUNNING 实例。缺的是:①网关不路由 `/videos`;②round-robin **负载盲**,会往满实例灌;③多实例轮询**无亲和**(串);④实例死**丢队列**。

**M4a — video 任务表(✅ 已完成)**:`schemas/video_generation_task.py` + 迁移。作**亲和映射 + 死亡重派持久化**(不是中央队列);状态机 QUEUED→ASSIGNED→RUNNING→DONE/FAILED/CANCELED。

**M4-1 — `LeastPendingStrategy`(`http_proxy/strategies.py`)**:替代 round-robin 的负载感知 LB。选**在飞任务(state∈{ASSIGNED,RUNNING})最少**的 RUNNING 实例——数 M4a 表 `GROUP BY instance_id`(一条 SQL,零额外网络往返;因所有流量经门面,账 = 引擎队列)。最空的也满 → 返 503。

**M4-2 — `/v1/videos` 薄门面(`routes/videos.py` 新建)✅ 已实现**:
- `POST /v1/videos`:解析 body(仅取 `model`/`task_type`/`user_id`,其余透传)→ 收输入字节落 NFS(§7.7,base64 落盘取本地路径 / URL 直透)→ **least-pending 选实例** → submit 到引擎 `/v1/tasks/{image,video}/`(按 `task_type`:`t2i`/`i2i`→image,余→video)→ 记 M4a 映射(`instance_id`/`native_task_id`,state=ASSIGNED)→ 返公开 `task_id`(job id,与 native id 解耦以便死亡重派换实例)。**无 GPUStack 侧参数校验**(new-api 校)。引擎 503(队列满)原样透传给 new-api 自限流。
- `GET /v1/videos/{id}`:查 M4a 映射 → **poll-on-GET**(现问该实例 `/v1/tasks/{native}/status`,映射引擎态→我方状态机)→ 更新行 → 返 state + `nfs_path`。**无后台完成轮询器**(new-api 每 15s 来时现问)。实例已非 RUNNING 则不轮询,留给 sweeper 重派;引擎 404(丢了 native id)当即标回 QUEUED。
- `GET /v1/videos/{id}/content`:DONE 后从 `nfs_path` 流式(`FileResponse` + 输出根前缀防穿越校验)。
- **输出根**:Config `lightx2v_output_root`(默认 `/nfs-output`,env 走标准 `GPUSTACK_LIGHTX2V_OUTPUT_ROOT`,运行时可经 /config 改;任务行记录创建时的 root,content 校验用记录值);门面按 §7.2 拼绝对 `save_result_path` 下发,引擎写此绝对路径(`file_service.get_output_path` 尊重绝对路径,已核),server 直接读回。
- **路由挂载**:`routes/routes.py` 把 `videos.router` 挂到 `inference_router`(前缀 `/v1`,同 `rerank`)。**故意不加进 `openai_model_prefixes`** —— 门面要做 NFS I/O + least-pending + 亲和记账,不能在网关层完成;加进去会被 AI-proxy 直连实例、绕过门面。gateway 启用时 `/v1/videos` 走通用 server 回退路由(同 `/v1/models` 等 server-native 端点)。
- 复用 `get_running_instances`、`select_least_pending_instance`(M4-1)、`model_instance_prefix`+`router_header_key`+`request_to_worker`(server→worker→实例转发,同 `openai.py`)。

**M4-3 — 死亡重派(极小 APScheduler-式 sweeper)✅ 已实现**:`server/video_task_sweeper.py`,leader-only(接 `_start_leader_tasks`),5s 一轮:①非终态(ASSIGNED/RUNNING)任务的映射实例已非 RUNNING → 标回 **QUEUED**(清亲和);②所有 QUEUED 用 `redispatch_task`(least-pending 重派,复用原 `params` 作引擎 body)。是唯一后台循环(门面主路径直派,无中央调度环)。图片同步无此需(同步失败 new-api 重试)。

**图片同步**:`/v1/images/generations` 已在 `openai_model_prefixes` + 引擎已实现 → 现有网关 round-robin 即可路由(可选:也走门面用 least-pending,统一但多一跳,按需)。

**砍掉 / 推迟**:中央 SKIP-LOCKED 主调度循环(主路径提交时直派,引擎队列缓冲)· 集群级限流 429(new-api 见 503 自限流 1 分钟)· 提交参数校验(new-api 做)。

> **NFS Janitor —— 已补做(2026-07-06)**:原计划推迟为 cron,后决定组件化(`server/video_storage_janitor.py`,leader-only,10min 一轮)。三重保护:①按天目录 TTL(读 Config `lightx2v_retention_days`)②水位驱逐(用量超 `high_watermark` → 最旧优先删到 `low_watermark`,cron 做不到的防写满)③安全门(不删今天目录 + 不删非终态任务所在目录)。使 P3 存储设置页的保留天数/水位**真生效**(不再是摆设),取代外部 `find -mtime` cron。

**部署要求**:GPUStack server 节点 + worker 节点挂 `/nfs-output`(fstab,与代码无关)。

### M5 — UI（gpustack-ui,独立仓：`/Users/reputationly/Desktop/code/api/gpustack-ui`）

> **实现状态(2026-07-06)**:P1 布线 ✅ · P2 播放页异步流 ✅ · P3 任务面板+存储设置页(含后端列表 API + Config 化输出根)✅。**均未本地编译验证**(无 pnpm/deps),留待出包 `pnpm build`。后端管理列表挂 `GET /v2/video-tasks`(owner/admin);面板用 `useTableFetch({polling:true})`(后端无 SSE watch 端点);存储设置走既有 `/config`(新增 `lightx2v_output_root`/`lightx2v_retention_days`/`lightx2v_storage_high_watermark`/`lightx2v_storage_low_watermark` 四个 Config 白名单字段,由 Janitor 强制执行)。**出包(扩 overlay)未做**。

> 探明(2026-07-06):视频 UI **已大量脚手架**——播放页目录 `src/pages/playground/video/{index,page,forms}` 已存在、路由 `config/routes.ts:81-91` 已写好只是注释掉、API 客户端 `CREATE_VIDEO_API=/v1/videos`+`createVideo()` 已定义。缺的是:①一堆枚举/布线没接 video;②播放页是「form-data 同步取 blob」的**半成品**(`use-text-video.ts:108` 直接把 `result.id` 当 dataUrl,无轮询),要改成我们门面的**异步作业流**;③任务面板/存储设置页是**全新页 + 需要新后端 API**。技术栈:Umi Max(@umijs/max)+ AntD 6,pnpm 9.3 / node 22,`npm run build`→`dist/`。
> **范围(已定):P1+P2+P3 全量**。**出包:扩 ACR overlay 工作流**(见下「出包」)。

**P1 — 布线(纯前端,枚举/路由/图标)**
- `config/routes.ts:81-91`:取消注释 video playground 路由。
- `src/pages/llmodels/config/index.ts`:`modelCategoriesMap` 加 `video:'video'`(:289 后);`categoryOptions` 加 `{label:'Video',value:...}`(:315 后);`backendLabelMap` 加 LightX2V(:53 后)。
- `src/pages/llmodels/constants/backend-parameters/index.ts:10`:`backendOptionsMap` 加 `lightX2V:'LightX2V'`;若算 built-in 加进 `BuiltInBackendOptions`。
- `src/pages/backends/config/index.ts:25`:导入 LightX2V logo(需一张 `@/assets/logo/lightx2v.*`)+ 加进 `builtInBackendLogos`。
- `src/pages/playground/video/index.tsx:43`:取模型的 `categories` 从 `image` 改 `video`。
- 参数表单**无需改**:`forms/backend-parameters-list.tsx` 已由后端 `common_parameters` 动态驱动,M1 填的参数自动渲染。

**P2 — 播放页异步改造(核心真活)**
- `src/pages/playground/apis/index.ts`:`createVideo` 改 **JSON**(`Content-Type: application/json`,body=`{model,task_type,prompt,...}`);加 `getVideo(id)`→`GET /v1/videos/{id}`、`getVideoContent(id)`→`GET /v1/videos/{id}/content`(responseType blob)。
- `src/pages/playground/hooks/use-text-video.ts`:重写提交/消费——POST 拿 `task_id` → **轮询** `getVideo` 到 `status∈{done,failed}`(间隔 ~2s,复用 `createAxiosToken` 支持取消)→ DONE 后 `getVideoContent` 取 blob → `URL.createObjectURL` → `videoList[0].dataUrl`;FAILED 显示 `error`。
- `src/pages/playground/video/page.tsx`:表单字段对齐引擎(size/width/height → `aspect_ratio`/`target_video_length`/`resize_mode`);`viewCode` 的 `isFormdata:true` 改 false。

**P3 — 任务面板 + 存储设置页（含新后端 API)**
- **后端·任务列表 API**(`gpustack/routes/videos.py`):加 `GET /v1/videos`(分页,返回 `VideoTasksPublic`;owner/admin 作用域,复用 `_authorize`);UI 用 `useTableFetch` 的 **`polling:true`(5s)**,**不建 watch/chunk 端点**。
- **后端·存储设置**:把 lx2v 输出根/保留天数/高低水位做成 **`Config` 字段 + `WHITELIST_CONFIG_FIELDS`**(`gpustack/utils/config.py`),经既有 `/config` GET/PUT 读写(admin 作用域已在);`videos.py` 的 `_OUTPUT_ROOT` 从 `os.environ` 改读 Config(小重构,env 作默认值回退)。**不建 DB 设置表**,与现有机制一致。
- **前端·任务面板**:新页 `src/pages/.../video-tasks.tsx`,仿 `resources/components/workers.tsx` 的 `useTableFetch({fetchAPI:queryVideoTasks, polling:true})`;列:task_id/model/user_id/state/created;操作:查看/下载(`/content`)。加路由。
- **前端·存储设置页**:新页,仿 `profile/components/settings-section.tsx`,表单字段 NFS 输出根/保留天数/水位,读写 `/config`。加路由。

**出包(扩 ACR overlay 工作流,已定)**
- **后端 overlay**:`pack/Dockerfile.acr` 的 COPY 列表**补齐 M4/M5 后端新文件**(`routes/videos.py`、`schemas/video_generation_task.py`、`server/video_task_sweeper.py`、`http_proxy/strategies.py`、迁移、`routes/routes.py`、`server/server.py`、`schemas/__init__.py`、`utils/config.py`、`scheduler/evaluator.py`);删掉「M4 不覆盖」旧注释。迁移在 server 启动时自动 `alembic upgrade head`,无需额外步骤。
- **UI overlay**:`.github/workflows/pack-acr-overlay.yml` 加步骤:checkout `gpustack-ui` → `pnpm i && pnpm build` → 把 `dist/` COPY 进镜像 `gpustack/ui/`(覆盖 base 里 COS 拉的官方 UI)。多架构随现有 buildx+QEMU。
- 注意:base 镜像 `gpustack/ui` 是 COS 官方预构建包,我们的 fork UI **叠加覆盖**;`_OUTPUT_ROOT` 若改 Config,server 侧需配 `--lx2v-output-root` 或环境默认。

**验证**:①部署界面原生显示 video 类目 + LightX2V 后端 + 参数表单;②体验区提交→轮询→出视频;③任务面板列出/下载;④设置页改输出根生效;⑤`pnpm build` 通过 + overlay 镜像起得来 + 自动迁移建表。

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
