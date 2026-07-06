# LightX2V M4+M5 交接文档（2026-07-06）

> 给接手 agent/工程师的完整交接。读完本文 + 引用的两份设计/计划文档,即可继续。
> **配套必读**:[`lightx2v-backend-design.md`](./lightx2v-backend-design.md)(设计,§6.0 收敛决策/§7 存储契约)· [`lightx2v-builtin-backend-plan.md`](./lightx2v-builtin-backend-plan.md)(里程碑计划,M4/M5 段有实现状态标注)· [`lightx2v-gpustack-部署实录.md`](./lightx2v-gpustack-部署实录.md)(§17 出包+部署 runbook、坑列表)。

---

## 1. 一句话背景

把 **LightX2V**(团队主力视频/图像生成引擎)做成 GPUStack **内置一等公民后端**。M1–M3(枚举/登记/后端模块/调度/类目门控)**已 commit 并真机出图成功**(commit `3106faa0` 起);本次交接的是 M4(异步作业门面)+ M5(UI)——**代码全部完成、过了 5 轮 Codex review,但全部未 commit、未出包、未真机验证**。

## 2. 三仓状态总览

| 仓 | 路径 | 状态 |
|---|---|---|
| **gpustack**(后端) | `/Users/reputationly/Desktop/code/api/gpustack` | M4+M5 后端**已 commit 并 push main**(2026-07-06:`808e8796` feat + `7847dba1` ci);M1–M3 已 commit 已部署 |
| **gpustack-ui**(前端) | `/Users/reputationly/Desktop/code/api/gpustack-ui` | M5 前端**已 commit 并 push main**(2026-07-06:`54a7ff64`);编译已验证(本地 + CI:`pnpm@9.3.0 install && pnpm build` 通过,dist 26M;`tsc --noEmit` 与 HEAD 基线对比无 M5 引入的新类型错误——仅新文件继承了仓库既有的 core-ui 无类型声明 TS7016) |
| **LightX2V**(引擎) | `/Users/reputationly/Desktop/code/api/LightX2V` | 干净,已 push(`ff2beee3`);launcher + profiles 已进 arm64 镜像 |

## 3. M4 —— 收敛后的异步门面(后端,已完成)

**收敛决策**(设计 §6.0,已实测三仓队列后拍板):重 dispatcher(中央 SKIP-LOCKED 调度循环/集群限流/参数校验)是 300 节点伪需求,**砍**。落地的是「薄门面 + least-pending + 亲和映射 + 死亡重派」:

| 组件 | 文件 | 要点 |
|---|---|---|
| M4a 任务表 | `gpustack/schemas/video_generation_task.py` + 迁移 `2026_07_05_2230-e1f2a3b4c5d6` | 亲和映射(公开 task_id ↔ instance_id+native_task_id)+ 状态机 QUEUED→ASSIGNED→RUNNING→DONE/FAILED/CANCELED;`owner_user_id`=GPUStack principal(鉴权),`user_id`=new-api 终端用户(路径用) |
| M4-1 负载感知 LB | `gpustack/http_proxy/strategies.py` | `select_least_pending_instance()`:数任务表在飞(ASSIGNED/RUNNING)`GROUP BY instance_id` 选最空实例,平局随机 |
| M4-2 门面 | `gpustack/routes/videos.py` | `POST /v1/videos`(base64/URL 输入落 NFS→least-pending→提交引擎 `/v1/tasks/{image,video}/`→记映射→返 task_id)· `GET /v1/videos/{id}`(**poll-on-GET**,无后台轮询器)· `GET .../content`(NFS 流式) |
| M4-3 死亡重派 | `gpustack/server/video_task_sweeper.py` | leader-only 5s 循环,四步:⓪非终态超 24h→FAILED(timeout) ①实例**连续 3 轮**非 RUNNING→在飞任务标回 QUEUED(防状态抖动误判;ASSIGNED 无 native id 超 120s 也回收=门面提交中途崩) ②在飞任务 updated_at 停滞超 10min→服务端主动向引擎查状态回填(客户端弃轮询兜底) ③QUEUED 走 `redispatch_task` 重派(重试上限 5,引擎 4xx 拒绝立即 FAILED,503/无实例不耗额度;**每次重派换新输出路径 `-r{n}` 后缀**,防误判重派时新旧引擎写同一文件) |
| Janitor(原推迟,后补做) | `gpustack/server/video_storage_janitor.py` | leader-only 10min:①按天目录 TTL ②水位驱逐(超高水位→最旧优先删到低水位)③安全门(不删今天目录 + 不删非终态任务的输出**和输入**目录 + **DONE 后 6h 取件宽限**);全部 FS 操作走 `asyncio.to_thread` |

**关键设计判断(接手前必须知道,别"顺手修掉")**:
1. **`/videos` 故意不加进 `gateway/utils.py openai_model_prefixes`** —— 加了会被网关直连实例、绕过门面(NFS/记账/亲和都在 server 侧)。门面挂 `inference_router`(同 rerank)。
2. **公开 task_id 与引擎 native id 解耦** —— 重派换实例后 native id 会变,公开 id 必须稳定。
3. **枚举列存的是成员名**(`'ASSIGNED'`),查询必须用**枚举成员**、不能用 `.value`('assigned' 匹配不到任何行)—— 已踩过一次(Codex 第 2 轮)。
4. **输出根**:Config `lightx2v_output_root`(默认 `/nfs-output`;env 走 BaseSettings 标准名 `GPUSTACK_LIGHTX2V_OUTPUT_ROOT`,旧的 `GPUSTACK_LX2V_OUTPUT_ROOT` 已删)。任务行记录创建时的 `output_root`,content 防穿越校验用**记录值**而非当前配置(运行时改 root 不会 400 历史结果)。引擎尊重绝对 `save_result_path`(已核 `file_service.get_output_path`),但**不建父目录**,门面/重派提交前 `_ensure_parent_dir`。
5. **引擎原生路径字段(`image_path` 等 + `save_result_path`)绝不透传**(`_ENGINE_OWNED_FIELDS`)——外部输入只能 base64/URL,防 IDOR。
6. `task_type` 白名单 `{t2i,i2i,t2v,i2v,flf2v,s2v}`(路径第一段,防穿越);t2i/i2i→image 端点,其余→video。
7. 提交失败分流:引擎 503(队列满)→503 透传(new-api 见 503 自限流 1min);≥500→503;真 4xx→400。
8. 管理列表 `GET /video-tasks` 挂 `v1_base_router`,**实际前缀是 `/v2`**(`versioned_prefix="/v2"`),owner/admin 作用域;门面 GET/content 有 `_authorize_task`(非 owner 非 admin→404 不泄露存在性)。

## 4. M4+M5 后端未 commit 文件清单(gpustack 仓)

```
新增:
  gpustack/schemas/video_generation_task.py        # M4a 表 + ListParams
  gpustack/migrations/versions/2026_07_05_2230-e1f2a3b4c5d6_add_video_generation_tasks.py
  gpustack/routes/videos.py                        # 门面(3 路由)+ redispatch_task
  gpustack/routes/video_tasks.py                   # GET /v2/video-tasks 管理列表
  gpustack/server/video_task_sweeper.py            # 死亡重派
  gpustack/server/video_storage_janitor.py         # TTL+水位+安全门
修改:
  gpustack/http_proxy/strategies.py                # LeastPendingStrategy
  gpustack/routes/routes.py                        # 挂 videos(inference)+ video_tasks(management)
  gpustack/server/server.py                        # sweeper+janitor 接进 _start_leader_tasks
  gpustack/schemas/__init__.py                     # 注册表(noqa F401)
  gpustack/config/config.py                        # 4 个 lx2v Config 字段
  gpustack/utils/config.py                         # 白名单 +4
  docs/ 三份文档                                   # 状态同步
```
迁移 revision `e1f2a3b4c5d6`,down_revision `c4d7e8f9a0b1`;server 启动自动 `alembic upgrade head`,**镜像里带上迁移文件即自动建表,无需手动步骤**。

## 5. M5 —— UI(gpustack-ui 仓,代码完成、未编译)

| 档 | 内容 | 文件 |
|---|---|---|
| P1 布线 | video 播放路由取消注释;类目 `video`;后端 `lightX2V:'LightX2V'`(backendOptionsMap+backendLabelMap);logo(已抠白底 264×76);播放页取模型 image→video | `config/routes.ts` · `src/pages/llmodels/config/index.ts` · `.../constants/backend-parameters/index.ts` · `src/pages/backends/config/index.ts` · `src/assets/logo/lightx2v.png` · `src/pages/playground/video/index.tsx` |
| P2 播放页异步流 | `createVideo` 改 JSON;新增 `getVideoTask`/`getVideoContent(blob)`;`use-text-video.ts` 重写为 POST→2s 轮询→DONE 拉 blob→objectURL(revoke 防漏,isStale 防竞态);viewCode isFormdata→false | `src/pages/playground/apis/index.ts` · `src/pages/playground/hooks/use-text-video.ts` · `src/pages/playground/video/page.tsx` |
| P3 面板+设置 | 任务面板(`useTableFetch({polling:true})`,**后端无 SSE watch,别用 watch:true**;下载按钮 done 才可点);存储设置页(读写 `/config`:输出根/保留天数/高低水位,**Janitor 真执行,非摆设**);路由挂 Resources 组;locale `lightx2v.ts`(en+zh,`require.context` 自动注册,menu.ts 手动加了 2 键) | `src/pages/video-tasks/` · `src/pages/storage-settings/` · `src/config/settings.ts`(PaginationKey)· locales |

**注意**:参数表单**无需改**——`forms/backend-parameters-list.tsx` 已由后端 InferenceBackend 行的 `common_parameters` 动态驱动。logo 暗色主题下 "Light" 黑字看不清,用户已拍板**不处理**。

## 6. Codex review 历史(5 轮 12 个真问题,全部已修)

| 轮 | 发现(全部实锤) |
|---|---|
| 1 | 迁移缺 `deleted_at` 列(ORM 映射了,不修则表一用就崩)· sweeper 只剩 QUEUED 时提前 return 死角 · `task_type` 路径穿越 |
| 2 | 枚举 `.value` vs 成员名不匹配 ×4 处(least-pending 恒 0、sweeper 恒空,**静默失效型**) |
| 3 | 输出父目录未创建(每个当天/用户/模型首提交必 FileNotFoundError)· GET/content 无归属校验(加 `owner_user_id`) |
| 4 | 引擎 5xx 被压成 400(new-api 会当不可重试) |
| 5 | 引擎原生路径字段透传(IDOR)· Janitor 删活任务的输入目录 |

**流程约定(用户要求)**:每轮改动过 `/codex:review` + 用户确认;**用户会做最终统一检视后才 commit,不要替他 commit**。

## 7. 验证状态(诚实清单)

| 项 | 状态 |
|---|---|
| 后端 flake8/black(改动文件) | ✅ 全绿(`config.py` 两个 C901 是**预先存在**,非本次引入) |
| 后端导入/路由/字段/SQL bind 自检 | ✅(枚举 bind 实测渲染 `IN ('ASSIGNED','RUNNING')`) |
| `make test` / pytest | ❌ 未跑(`uv run` 触发 depsync,当时网络拉 selectolax 超时;uv 在 `/opt/anaconda3/envs/gpustack/bin/uv`) |
| 前端 TS 编译(`pnpm build`) | ❌ 未验证(本机无 pnpm;node v26 在 `/opt/homebrew/bin/node`;仓要求 pnpm@9.3/node 22)。import 路径/类型均对照代码核过,但 TS 严格性只能编译暴露 |
| 真机端到端(门面提交/亲和/杀实例重派/Janitor) | ❌ 未做,依赖出包 |

## 7.5 第 6 轮检视(Claude /code-review,2026-07-06)—— 已全部修复

高强度 8 角度 review 确认 10 项 + 2 项顺手修,**全部已改完**(lint/导入/单元断言已过):

1. **迁移 `state` 列改原生 `sa.Enum(name='videotaskstateenum')`**(原 VARCHAR 在 postgresql+asyncpg 下 bind cast 到不存在的类型,**PG 上功能整体不可用**;已断言 ORM/迁移对齐)。同迁移顺带 `ALTER TYPE operationenum ADD VALUE 'VIDEO_GENERATION'`(见第 12 条)。
2. **重派重试上限**:`redispatch_task` 分三类——引擎 4xx(非 503)→立即 FAILED(dispatch_rejected);503/无实例→不耗额度等下轮;瞬时错误→耗 1 次额度,上限 5(retry_exhausted)。
3. **防状态抖动重复派发**:sweeper 连续 3 轮未见 RUNNING 才 requeue + 每次重派换 `-r{n}` 新输出路径(nfs_path/params 同步更新)。
4. **僵尸任务回收**:sweeper 新增停滞超 10min 的在飞任务服务端主动查引擎回填 + 非终态超 24h 硬超时 FAILED(否则永久虚占 least-pending 计数、永久钉住 janitor 保护目录)。
5. **先落库后提交引擎**:POST 先建 ASSIGNED(native_task_id=None)行→提交→成功补 native id/失败删行(原顺序 DB 插入失败会留下无人知晓的孤儿引擎任务)。
6. **GET 轮询不再跨 worker HTTP 持连接池**:读完即关 session,`_poll_and_fold` 重构为无 session 的 `fetch_engine_status_updates`(门面与 sweeper 共用),更新走短 session(对齐 openai.py 惯例)。
7. **janitor 全部 NFS IO(glob/rmtree/disk_usage)套 `asyncio.to_thread`**(原直接跑在 API 主事件循环上,大目录 rmtree 会冻住整个 server)。
8. **`coerce_value_by_field` 加 None/空串守卫**(PUT /config 传 null(文档允许的"禁用"语义)原来直接 int(None) 500 整个更新)。
9. **DONE 任务取件宽限**:janitor 保护集追加"DONE 且 updated_at 6h 内"(原水位驱逐只跳"今天",昨天 23:59 完成未取件的结果 00:05 就可能被删)。
10. **content 防穿越校验用任务行记录的 `output_root`**(新列)而非当前配置(运行时改 root 不再 400 历史结果);`_output_root()` 删掉自造 env `GPUSTACK_LX2V_OUTPUT_ROOT`,Config 默认 `/nfs-output`,env 走标准 `GPUSTACK_LIGHTX2V_OUTPUT_ROOT`。
11. 顺手:requeue 日志先存 dead_instance_id 再 update(原永远打 "instance None");水位判空改 `is None`(0.0 是合法水位)。
12. 顺手:视频请求接入用量统计——`_resolve_target_model` 设 `request.state.model`/`model_route_id`,`ModelUsageMiddleware` 增加 `POST /v1/videos` 分支(OperationEnum 新增 `VIDEO_GENERATION`)。

## 8. 待办(按优先级,接手从这里开始)

1. ~~【必做】扩 ACR overlay 出包~~ **✅ 已完成(2026-07-06)**:
   - `pack/Dockerfile.acr` 已补齐全部 M4/M5 后端文件(含迁移,启动自动 upgrade)+ `pack/ui-dist/` UI 覆盖机制;
   - `pack-acr-overlay.yml` 已加 checkout `gpustack-ui` → pnpm build → dist 进镜像步骤(`ui_ref` 输入,默认 main);
   - 首次构建成功(run 28775282654):`gpustack:lx2v-dev` + 不可变 tag **`lx2v-20260706-0731-7847dba1`** 已推 ACR,双架构,镜像内断言(backend/M4 schema/UI dist)全过。
2. **【必做】真机部署验证**(238 x86 server / 163 arm64 4×A100 worker,网络隔离,流程见部署实录 §17):升级两节点镜像 → 确认自动迁移建表 → `POST /v1/videos`(z_image t2i)→ 轮询 → `/content` 下载;并发提交看 least-pending;杀实例看重派;UI 看 video 类目/播放页/任务面板/存储设置。**注意 GPU 外部占用坑**(此前有残留进程占 21G 导致 OOM,先 `nvidia-smi` 清场)。
3. ~~【必做】用户统一代码检视 → 分批 commit~~ **✅ 已完成(2026-07-06)**:第 6 轮检视(§7.5)修复后,gpustack `808e8796`+`7847dba1`、gpustack-ui `54a7ff64` 均已 push main。
4. 【可选】再跑一轮 `/codex:review` 确认第 5 轮两个 fix 收敛(轮次命中在收敛:3→2→2→1→2)。
5. 【后续】new-api 侧 GPUStack channel + TaskAdaptor(`BuildRequestURL→/v1/videos`,`FetchTask→GET /v1/videos/{id}`,完成后读 `nfs_path` 直传 OBS)——设计 §6.9,**new-api 仓改动,本次未动**;new-api 仓有"Codex review + 用户确认才能 commit"的硬约定。
6. 【后续】播放页表单字段与引擎精细对齐(`aspect_ratio`/`target_video_length`;当前透传+默认可用,低优先);LightX2V 品牌 logo 若有正式版可替换。

## 9. 环境速查

- 本机 python 工具链:`/opt/anaconda3/envs/gpustack/bin/{python,uv,black,flake8}`(PATH 里默认没有);PIL 在 `/opt/anaconda3/bin/python`。
- lint:black 88 + skip-string-normalization;flake8 E501 忽略、复杂度 pre-commit 15/CI 10;`migrations/` 不 lint。
- 部署:两台机(238 管理 x86 / 163 计算 arm64 4×A100),无公网;镜像走 ACR 或 `docker save`+NFS 搬运(**坑#5:失败的 `save -o` 会留隐藏 `.tmp-*` 半截文件**);模型权重 `/nfs-models`,生成物 `/nfs-output`(worker 经 `GPUSTACK_EXTRA_MOUNTS=/nfs-output` 挂进引擎容器,server 直挂同路径)。
- 引擎镜像:`.../reputationly/lightx2v:arm64-a100-latest`,entrypoint 含 `gpustack-lx2v-launcher`(前置代理:自答 `/ready`,内部起 lightx2v.server/torchrun,profile 在镜像内 YAML)。
- 引擎 API:每实例进程内 FIFO(max 10,满→503,串行);`POST /v1/tasks/{image,video}/` 返 `task_id`;`GET /v1/tasks/{id}/status` 状态 `pending/processing/completed/failed/cancelled`(注意双 L,已映射);task_id 内存态,实例重启即失(死亡重派存在的原因)。
