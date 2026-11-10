# GPUStack fork 升级到上游 v2.3.0：开发与自测文档

- 适用仓库：`reputationly/gpustack`（后端）和 `reputationly/gpustack-ui`（前端）
- 读者：负责实施这次升级的开发 agent；验收由本文档作者按 §10 进行
- 文档日期：2026-11-10
- 配套文件：同目录 `ref-patches/`（参考补丁与新增测试，见附录 A）

## 1. 目标与范围

把 fork 从上游 `v2.2.3` 升到上游正式版 `v2.3.0`，保留 fork 的全部自研能力，并把以后跟上游的成本降下来。

**在范围内**
- 后端：`git merge v2.3.0`，解决 14 个冲突文件，处理 4 处"Git 不报冲突但会坏"的问题（选择器、迁移链、overlay 构建、内部文档位置）。
- 前端：`gpustack-ui` fork 同步到 UI 的 `v2.3.0`。
- 按 §8 自测并提交自测报告。

**不在范围内（不要做）**
- 不追 `upstream/main` 或 `v2.3.1rc1`。
- 不用 rebase，不改写 `main` 的历史，不 force-push。
- 不动 Mooncake：m 集群 worker 上的 `mooncake_client` 和 vLLM 的 `--kv-transfer-config` 保持现状。
- 不做架构重构（7 个后端参数化、视频子系统搬进 extension plugin 等）。这些是后续工作，混进来会让这次升级无法验收。
- 不调整上游 `build_candidate_selector()` 里已有分支的顺序（见 §6 已知限制）。
- 不手改 `gpustack/client/generated_*.py`，不改我方已有的 4 个迁移文件。
- 不直接合进 `main`，不动 m 集群。交付物是两个分支上的 PR 和自测报告。

## 2. 基线事实

| 项 | 值 |
|---|---|
| fork `origin/main` | `8b02caf3`（2026-10-18） |
| fork 当前基于 | 上游 `v2.2.3`（`hack/upstream-base.env`） |
| 升级目标 | 上游 `v2.3.0` = `63a03ba1`（2026-10-22） |
| 共同祖先 | `6047541f`（`v2.2.2` 之后 1 个提交；`v2.2.3` 在上游的发布分支上，不是 `v2.3.0` 的祖先，所以有几个我方没改过的文件也报冲突） |
| 上游 `v2.2.3`→`v2.3.0` | 454 个提交，1386 个文件 |
| fork 相对 `v2.2.3` | 99 个非 merge 提交，94 个文件 |
| 后端试合并 | 14 个冲突文件 |
| 前端 | fork = UI `v2.2.3`（`29b965eb`）+ 26 个提交；目标 UI `v2.3.0` = `7a710563`；5 个冲突文件 |
| 迁移链 | fork：`c4d7e8f9a0b1` → `e1f2a3b4c5d6` → `f3a1b2c4d5e7` → `a7c3e5d19b42` → `b8d4f6a2c1e9`；上游：`c4d7e8f9a0b1` → …（9 个）… → `a7e2c9d4f681` |

开工前先 `git fetch origin && git fetch upstream --tags`，确认 `origin/main` 还是 `8b02caf3`。如果 `main` 已经前进，新提交若碰了 §5 列出的文件，先停下来报告，不要自行判断。

## 3. 总体做法

### 3.1 分支

```bash
# 后端
git remote add upstream https://github.com/gpustack/gpustack.git 2>/dev/null || true
git fetch origin && git fetch upstream --tags
git checkout -b chore/upstream-v2.3.0 origin/main
git merge --no-ff --no-commit v2.3.0
```

本文档和 `ref-patches/` 在后端仓库的分支 `docs/upgrade-v2.3.0-dev-guide` 上，路径 `docs-fork/upstream-sync/`。提交 merge commit 之后，把这个目录带进升级分支单独提交（`git checkout origin/docs/upgrade-v2.3.0-dev-guide -- docs-fork/upstream-sync && git commit -m "文档：v2.3.0 升级开发与自测文档"`），让文档随升级一起落地。

前端同名分支 `chore/upstream-v2.3.0`，见 §7。

### 3.2 提交拆分

为了能逐块验收，按下面顺序提交，不要压成一个提交：

| 序号 | 提交内容 | 对应章节 |
|---|---|---|
| 1 | merge commit：只包含解冲突（14 个文件）和让代码能 import 的最小改动（`serve_manager.py` 的 6 处自动合并残留、`lightx2v_resource_fit_selector.py`） | §5 A / B / C 组、D1 |
| 2 | 迁移 merge revision + `tests/migrations/test_single_head.py` + 3 个测试 fixture | D2 |
| 3 | overlay：`pack/Dockerfile.acr`、`hack/upstream-base.env`、`pack-acr-overlay.yml` | D3 |
| 4 | 内部文档搬到 `docs-fork/` + `mkdocs.yml` 取上游 + 路径引用改名 + `AGENTS.md` 的 Fork notes | D4、A2 |
| 5 | 新增测试：`tests/policies/test_fork_selectors.py`、`tests/server/test_builtin_backend_backfill.py`、`test_serve_manager.py` 里我方的 5 个用例（如未随提交 1 进入） | §5 各节 |
| 6 | 自测报告 | §8.5 |

提交 1 之后 `uv run python -c "import gpustack.server.server, gpustack.worker.serve_manager, gpustack.scheduler.scheduler"` 必须成功。

### 3.3 冲突文件分类总览

| 组 | 含义 | 文件 |
|---|---|---|
| A | 取上游 | `gpustack/gateway/__init__.py`、`pyproject.toml`、`uv.lock`、`CLAUDE.md`（内容并入 `AGENTS.md`）、`.github/workflows/pack.yaml`（加 1 行守卫） |
| B | 机械合并（两边都要，互不相干） | `.dockerignore`、`gpustack/routes/benchmarks.py`、`gpustack/http_proxy/strategies.py`、`tests/worker/test_serve_manager.py`、`tests/worker/backends/test_backend.py` |
| C | 语义冲突（上游重写了同一段逻辑） | `gpustack/scheduler/scheduler.py`、`gpustack/server/controllers.py`、`gpustack/server/server.py`、`gpustack/worker/serve_manager.py` |
| D | Git 不报冲突，但不处理会坏 | D1 七个选择器；D2 迁移链；D3 overlay 构建；D4 内部文档位置 |

### 3.4 通用规则

1. 冲突解法的总原则：**上游的结构为主体，我方行为挂上去**。任何"整段取我方"的做法都要在自测报告里说明理由。
2. 每个文件解完就跑该节末尾的"自检"命令。
3. `ref-patches/` 里的补丁是我在草稿 worktree 上按本文档解法做出来并跑过测试的结果，用来对照，不是让你 `git apply` 了事：`main` 可能已经前进，逐块读懂再落。你的结果和参考补丁有出入时，在自测报告的"与文档不一致"一节写明。
4. Python 一律用 `uv run`。改了 schema 就 `make generate`。
5. 提交信息用中文。

## 4. 实施步骤

| 步骤 | 做什么 | 完成标志 |
|---|---|---|
| 1 | 建分支、`git merge --no-ff --no-commit v2.3.0` | `git diff --name-only --diff-filter=U` 列出 14 个文件 |
| 2 | A 组 | A 组自检通过 |
| 3 | B 组 | B 组自检通过 |
| 4 | C1 `scheduler.py` + D1 选择器 | `tests/policies/test_fork_selectors.py` 27 passed |
| 5 | C2 `controllers.py`、C3 `server.py` | 自检通过；backfill 测试 4 passed |
| 6 | C4 `serve_manager.py` + B4 测试 | `tests/worker/test_serve_manager.py` 207 passed |
| 7 | `uv sync`，提交 merge commit | §3.2 的 import 检查通过 |
| 8 | D2 迁移 | `tests/migrations` 全过；§8.2 的 D1–D4 通过 |
| 9 | D3 overlay | `hack/check-overlay-list.sh` OK；§8.3 的 I2、I3 通过 |
| 10 | D4 文档搬家、A2 的 `AGENTS.md` | `tests/hack` 616 passed |
| 11 | `make install && make lint && make test` | 0 failed |
| 12 | 前端 §7 | U1–U4 通过 |
| 13 | 推两个分支、各开一个 PR（不合并），触发 overlay 构建（`ui_ref` 指向前端升级分支） | I4、I5 |
| 14 | devm 部署，跑 §8.4 | 自测报告 |

## 5. 逐文件解法

### A 组：我方相对 v2.2.3 没有任何改动的文件 → 整文件取上游

这 3 个文件报冲突，只是因为 v2.2.3 是上游发布分支上的补丁版本，不是 v2.3.0 的祖先（共同祖先是 `6047541f`）。已用 `git diff v2.2.3 origin/main -- <file>` 确认三者输出都是 0 行，我方没有自己的修改。

```bash
git checkout --theirs -- gpustack/gateway/__init__.py pyproject.toml uv.lock
git add gpustack/gateway/__init__.py pyproject.toml uv.lock
```

| 文件 | 冲突处数 | 说明 |
|---|---|---|
| `gpustack/gateway/__init__.py` | 5 | 我方侧是 v2.2.3 的插件版本号（1.0.1 等），上游改成了 `plugin_spec_overrides(...)` 并删掉了 `model_mapper_plugin()` |
| `pyproject.toml` | 1 | 三个依赖版本：`gpustack-runner 0.1.31.post4`、`gpustack-runtime 0.2.6`、`gpustack-higress-plugins 0.5.0.post2` |
| `uv.lock` | — | 取上游后执行 `uv lock --check`，必须通过且**不产生 diff**。我方没有新增 Python 依赖，所以不需要重新生成 |

自检：
```bash
git diff v2.3.0 -- gpustack/gateway/__init__.py pyproject.toml uv.lock | wc -l    # 期望 0
uv lock --check                                                                   # 期望退出码 0
```

### A2 `CLAUDE.md`（两边都新增了同名文件）

上游的 `CLAUDE.md` 只有一行 `@AGENTS.md`，内容搬到了新文件 `AGENTS.md`（162 行）。我方的 `CLAUDE.md` 是 100 行的自写说明。

解法：`CLAUDE.md` 取上游（一行 `@AGENTS.md`）；在 `AGENTS.md` **末尾**追加一节 `## Fork notes (reputationly)`，只放我方独有的内容：
- 指向 `docs-fork/secondary-development-pipeline.md` 的那一段（路径随 D4 改名）（我方 `CLAUDE.md` 第 9 行）。
- 一行指向本文档：`Upstream sync procedure: docs-fork/upstream-sync/upgrade-v2.3.0-dev-guide.md`。
- 我方后端清单与"新增后端要改哪些文件"的说明（如我方 `CLAUDE.md` 里有）。

通用内容（命令、约定、生成代码说明）以上游 `AGENTS.md` 为准，不要重复。

### A3 `.github/workflows/pack.yaml`（1 处）

我方把触发条件删成只剩 `workflow_dispatch: {}`（fork 没有上游的镜像仓库密钥，自动触发每次都失败）；上游修改了被我方删掉的那段 `push` / `pull_request` 的 `paths` 列表。

**不能简单取我方**：上游 v2.3.0 新增的 `tests/hack/test_docs_packaging.py` 会读这个文件的 `on.pull_request.paths`，取我方会有 18 个测试 `KeyError: 'pull_request'`（试合并实测）。

解法：
1. `on:` 整块**取上游原样**（`workflow_dispatch` / `repository_dispatch` / `push` / `pull_request` 全部保留）。
2. 把 `on:` 上方我方的 Fork note 注释改成说明新做法。
3. 在第一个 job `metadata` 上加一行守卫，其余 job（`build` → `merge` → `chart`）都直接或间接 `needs: metadata`，会一起跳过：

```yaml
jobs:
  metadata:
    if: ${{ github.repository == 'gpustack/gpustack' || github.event_name == 'workflow_dispatch' }}
    runs-on: ubuntu-22.04
```

效果与原来相同（fork 上的自动触发不再失败，手动触发仍可用），而文件与上游只差 7 行，以后同步不再在 `on:` 块冲突。

自检：
```bash
git diff v2.3.0 -- .github/workflows/pack.yaml | grep -c "^[+-][^+-]"      # 期望 7（6 行注释 + 1 行 if）
uv run pytest tests/hack/test_docs_packaging.py -q                           # 全过
```

### B1 `.dockerignore`（1 处，机械）

两边都在文件末尾追加条目，全部保留。合并后文件末尾应为：

```
.data

# CI-only staging dirs (pack-acr-overlay checks out gpustack-ui here; keep the
# docker build context lean)
gpustack-ui-src/

# Offline docs are built in their own stage.
gpustack/help/
mkdocs.offline.generated.yml
```

### B2 `gpustack/routes/benchmarks.py`（1 处，机械）

`from gpustack.schemas.models import (...)` 的导入列表里，我方加了 `is_music_model`、`is_video_model`，上游加了 `role_effective_model`、`servable_instances`。四个都要。解完后该导入块为：

```python
    is_audio_model,
    is_embedding_model,
    is_image_model,
    is_music_model,
    is_reranker_model,
    is_video_model,
    role_effective_model,
    servable_instances,
)
```

冲突块之后上游新增的那批 import（`Cluster`、`ModelRoute…`、`Principal`、`get_global_config`、`get_server_url`）是自动合并进来的，不要删。

自检：`grep -n "is_video_model\|is_music_model\|role_effective_model\|servable_instances" gpustack/routes/benchmarks.py`，四个名字都应同时出现在 import 和函数体里（各 ≥2 处）。

### B3 `gpustack/http_proxy/strategies.py`（1 处，机械 + 一处必须手改）

- 上游把 `RoundRobinStrategy` 改成按实例 id 轮询（`self._instance_ids`，迭代器里存 id，返回时再按 id 取实例）。
- 我方在同一位置之后追加了模块级函数 `select_least_pending_instance()`，并在文件头加了 import 和 `_tie_break_cursor`。

解法：`RoundRobinStrategy` 整个类**取上游**；我方的 `select_least_pending_instance()` **原样保留**在类之后；文件头的 import 和 `_tie_break_cursor` 保留我方。

冲突块解完后，`select_instance` 的结尾必须是上游的三行，不能是我方旧的 `return next(self._iterators[model_id])`（那样返回的是 int 不是实例）：

```python
        next_id = next(self._iterators[model_id])
        instances_by_id = {instance.id: instance for instance in instances}
        return instances_by_id[next_id]


async def select_least_pending_instance(
    session: AsyncSession, instances: List[ModelInstance]
) -> Optional[ModelInstance]:
    ...  # 我方函数体一字不改
```

另外检查 `__init__`：必须是上游的 `self._instance_ids: Dict[int, List[int]] = {}`，文件里不应再出现 `_instance_lists`。

自检：
```bash
grep -c "_instance_lists" gpustack/http_proxy/strategies.py        # 期望 0
grep -c "def select_least_pending_instance" gpustack/http_proxy/strategies.py   # 期望 1
grep -c "_tie_break_cursor" gpustack/http_proxy/strategies.py      # 期望 ≥4
uv run pytest tests/http_proxy -q
```

### B4 `tests/worker/test_serve_manager.py`（2 处）

- 冲突 1（import，L33）：取上游的整段 import，再补一行我方的 `from gpustack.worker.backends.base import InferenceServer`（`test_starting_snapshot_keeps_reserved_ports` 要用）。
- 冲突 2（文件末尾，L2413）：我方追加了 300 行（11 个测试 + 辅助函数 `_mp_vllm_model` 等），上游追加了 1491 行。**上游 1491 行全取**；我方 11 个测试按下表处理，保留的接在上游内容之后。冲突块开头那 3 行断言取上游版本（`written = …` 那 4 行）。

| 我方测试 | 处置 | 原因 |
|---|---|---|
| `test_follower_relaunches_when_main_process_group_changes` | **保留**，按新函数改写 | 对应移植 #3。改为直接调 `manager._start_or_restart_subordinate(mi)` 或经 `_dispatch_model_instance_event`，断言 `_stop_model_instance`、`_start_model_instance` 各被调用一次 |
| `test_follower_keeps_container_for_the_current_main_pid` | **保留**，同上改写 | 对应移植 #3 的反例：`main_launch_pid == mi.pid` 时不 stop、不 start |
| `test_starting_snapshot_keeps_reserved_ports` | **保留** | 测的是 `backends/base.py`，与 serve_manager 重构无关 |
| `test_sync_does_not_fail_a_follower_that_has_not_launched_yet` | **保留** | 对应残留 E |
| `test_starting_instance_does_not_inherit_follower_error` | **删除** | 对应残留 F，已还原成上游 |
| `test_assign_ports_tops_up_mp_slots_when_only_http_port_is_set` | **删除** | 对应行为 #4，不移植 |
| `test_assign_ports_keeps_a_complete_mp_list` | **删除** | 同上，上游有自己的端口测试 |
| `test_assign_ports_subordinate_does_not_invent_ports` | **删除** | 同上 |
| `test_port_publish_does_not_restart_the_start_in_progress` | **删除** | 对应 `_start_guard`（行为 #1、#2），已删 |
| `test_scheduled_update_skips_restart_while_provisioning` | **删除** | 同上 |
| `test_scheduled_update_restarts_when_not_provisioning` | **删除** | 同上，上游已有 SCHEDULED 重启的测试 |

**新增 1 个测试**（移植 #3 的策略分支）：`test_stale_follower_is_replaced_even_when_auto_restart_is_disabled` —— `_auto_restart_enabled` 打桩返回 `False`、`main_launch_pid != mi.pid`、容器状态 RUNNING，断言仍然 stop + start。

保留的测试如果依赖我方辅助函数（`_mp_vllm_model` 等），辅助函数一并保留；不再被引用的删掉。

自检：`uv run pytest tests/worker/test_serve_manager.py -q`，全部通过；`grep -c "_start_guard\|_ports_already_reserved" tests/worker/test_serve_manager.py` 期望 0。

### B5 `tests/worker/backends/test_backend.py`（1 处，机械）

两边都在文件末尾追加：我方 46 行（`/dev/shm` 上限的测试），上游 613 行。**两段都保留**，上游在前、我方在后，中间空两行。

自检：`uv run pytest tests/worker/backends -q`。

### C1 `gpustack/scheduler/scheduler.py`（2 处，语义冲突）

**背景**：上游把 `find_candidate()` 里"按后端挑选择器"的 `if/elif` 链抽成了模块级函数 `build_candidate_selector(config, model, model_instances, cpu_only=False, ram_claim=None, *, resource_view)`（v2.3.0 第 650 行），并且**所有选择器的构造函数都新增了必填的关键字参数 `resource_view`**。成组调度（PD）也调用这个函数来数容量，所以我方分支必须放进这个函数里，不能留在 `find_candidate()`。

**冲突 1（约第 579 行）**：我方只是把 `async def find_candidate(` 加了 `# noqa: C901`；上游在它前面插入了 317 行新函数（`apply_candidate_to_instance`、`_cards_per_member`、`build_candidate_selector`、`_group_scorer` 等）。
- 解法：**取上游整段**。`find_candidate` 的函数头用上游的（不带 `# noqa`）。合并后如果 flake8 报 C901 再加回 `# noqa: C901`，不报就不加。

**冲突 2（约第 1005 行）**：我方 57 行 `if/elif` 链 vs 上游 8 行调用。
- 解法：**冲突块取上游**，即：

```python
        candidates_selector = build_candidate_selector(
            config,
            model,
            model_instances,
            cpu_only=cpu_only,
            ram_claim=ram_claim,
            resource_view=resource_view,
        )
```

- 然后在 `build_candidate_selector()` 里，**`SGLANG` 分支之后、最后的 `return CustomBackendResourceFitSelector(...)` 之前**，插入我方 7 个分支（注意是 `if ... return`，不是 `elif`，并且都要传 `resource_view=resource_view`）：

```python
    if model.backend == BackendEnum.SGLANG:
        return SGLangResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    # --- fork backends (reputationly) ---------------------------------
    if model.backend == BackendEnum.LIGHTX2V:
        return LightX2VResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    if model.backend == BackendEnum.BERNINI:
        # Bernini: native Bernini-R video engine; multi-GPU Ulysses like
        # LightX2V (GPUs/replica from deploy input, see selector).
        return BerniniResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    if model.backend == BackendEnum.INDEXTTS:
        # IndexTTS-2: whole-GPU exclusive, 1 instance/card (see selector).
        return IndexTTSResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    if model.backend == BackendEnum.BREEZE_TTS:
        # Breeze TTS 2: whole-GPU exclusive, 1 instance/card (see selector).
        return BreezeTTSResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    if model.backend == BackendEnum.YUE2:
        # YuE2: whole-GPU exclusive, 1 instance/card (see selector).
        return YuE2ResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    if model.backend == BackendEnum.ACESTEP:
        # ACE-Step: whole-GPU exclusive, 1 instance/card (same as IndexTTS).
        return ACEStepResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    if model.backend == BackendEnum.VLLM_OMNI:
        # vLLM-Omni: whole-GPU exclusive; variable card count (1 for small
        # TTS, 2 for the 8B MOSS models via gpus_per_replica — see selector).
        return VLLMOmniResourceFitSelector(
            config, model, model_instances, resource_view=resource_view
        )
    # --- end fork backends --------------------------------------------
    return CustomBackendResourceFitSelector(
        config, model, model_instances, resource_view=resource_view
    )
```

- 位置约束（顺序有意义，不要挪）：
  1. 必须在 `if cpu_only:` 和 `if model.gpu_type_selector:` 两个上游分支**之后**。这两个分支是上游给 PD router 和 GPU 规格/切片用的，优先级高于后端。
  2. 必须在兜底的 `CustomBackendResourceFitSelector` **之前**。
- 文件头我方 7 个选择器的 import 是自动合并保留的，确认还在：`grep -n "LightX2VResourceFitSelector\|BerniniResourceFitSelector\|IndexTTSResourceFitSelector\|BreezeTTSResourceFitSelector\|YuE2ResourceFitSelector\|ACEStepResourceFitSelector\|VLLMOmniResourceFitSelector" gpustack/scheduler/scheduler.py`，每个名字应出现 2 次（import 1 次 + 使用 1 次）。
- `find_candidate()` 里不应再有任何 `candidates_selector = XxxResourceFitSelector(` 的直接构造。

### D1 我方 7 个选择器适配 `resource_view`（Git 不报冲突，但不改就跑不起来）

只需改一个文件：`gpustack/policies/candidate_selectors/lightx2v_resource_fit_selector.py`。其余 6 个选择器（acestep / bernini / breeze / indextts / vllm_omni / yue2）都继承它，且**没有**自己的 `__init__`，不用动。

改动 1：import。上游已删除 `gpustack.policies.utils.get_worker_allocatable_resource`（改成选择器基类上的方法）。把它从 `from gpustack.policies.utils import (...)` 里去掉，并新增 `ResourceView` 的 import（路径以上游 `custom_backend_resource_fit_selector.py` 文件头为准，照抄）。

改动 2：构造函数（第 55–57 行）。现在是：

```python
    def __init__(self, cfg: Config, model: Model, model_instances: List[ModelInstance]):
        super().__init__(cfg, model, model_instances)
        self._gpus_per_replica = self._resolve_gpus_per_replica(model)
```

改成（签名与上游父类 `CustomBackendResourceFitSelector.__init__` 对齐，透传全部参数）：

```python
    def __init__(
        self,
        cfg: Config,
        model: Model,
        model_instances: List[ModelInstance],
        cpu_only: bool = False,
        ram_claim: Optional[int] = None,
        *,
        resource_view: ResourceView,
    ):
        super().__init__(
            cfg,
            model,
            model_instances,
            cpu_only=cpu_only,
            ram_claim=ram_claim,
            resource_view=resource_view,
        )
        self._gpus_per_replica = self._resolve_gpus_per_replica(model)
```

改动 3：两处调用（第 134、204 行）。现在是：

```python
                allocatable = get_worker_allocatable_resource(
                    self._model_instances, worker, gpu_type
                )
```

改成基类方法（内部走 `self._resource_view.allocatable(worker, gpu_type)` 并带缓存）：

```python
                allocatable = self.get_worker_allocatable_resource(worker, gpu_type)
```

自检：
```bash
grep -rn "policies.utils import" -A6 gpustack/policies/candidate_selectors/lightx2v_resource_fit_selector.py | grep -c get_worker_allocatable_resource   # 期望 0
grep -c "self.get_worker_allocatable_resource(worker, gpu_type)" gpustack/policies/candidate_selectors/lightx2v_resource_fit_selector.py              # 期望 2
grep -rn "ResourceFitSelector(" gpustack --include=*.py | grep -v "resource_view" | grep -v "^gpustack/policies/candidate_selectors/.*class "        # 人工看：不应有漏传 resource_view 的构造
```

**新增测试 `tests/policies/test_fork_selectors.py`**（现在 `tests/` 下没有任何我方 7 个选择器的测试）。我已在试合并上写好并跑通（27 passed），文件在 `ref-patches/test_fork_selectors.py.ref`，直接拷进去。覆盖：

| 用例 | 断言 |
|---|---|
| `test_dispatch_returns_the_fork_selector` ×7 | `build_candidate_selector()` 对 7 个后端各返回对应类（`type(...) is cls`），且 `selector._resource_view is view` |
| `test_places_one_whole_gpu` ×7 | 8×80G 的 worker 上有候选，每个候选恰好 1 张卡 |
| `test_no_candidate_on_cpu_only_worker` ×7 | 纯 CPU worker 上无候选 |
| `test_multi_gpu_replica` ×3（LightX2V / Bernini / vLLM-Omni） | `gpus_per_replica=2` 时每个候选 2 张卡；4 卡 worker 放不下 8 卡副本 |
| `test_absolute_vram_floor_rejects_small_cards` ×2 | ACE-Step 的 30 GiB 下限、vLLM-Omni 多卡下限在 16G 卡上拒绝 |
| `test_reservations_in_the_resource_view_are_honoured` | 通过 `ResourceView([], {worker.id: Allocated(...)})` 预留了两张卡的一半显存后，这两张卡不再被选中（证明选择器确实走 `resource_view`，而不是自己重算） |

**已知限制（本次不解决，必须写进升级说明）**：`build_candidate_selector()` 里上游的 `if model.gpu_type_selector:` 分支排在所有后端分支之前。自研 7 个后端的模型如果在部署时选了 GPU 规格（上游 v2.3.0 新增的 instance type / 切片），会走上游的通用选择器，**绕过**我方整卡独占的规则；同文件 `_cards_per_member()` 的映射也只认 vLLM / MindIE / SGLang，其余按 1 卡算。本次升级的口径是：自研后端不支持与 GPU 规格同时使用。不要为此调整分支顺序（那会改变上游 PD / 切片的行为）。自测 S6 要求确认现网没有这种组合。

### C2 `gpustack/server/controllers.py`（1 处，比预想简单）

**实际情况**（逐行核对过合并结果）：上游**保留了** `_init_built_in_backends()`，我方的 `common_parameters` 回填就在这个函数的 `for` 循环末尾，落点还在。冲突的原因是：紧接其后，上游把 `_init_community_backends()` 整个删掉，换成了 `_reconcile()` 和 `_seed_builtin_source()`，两边改动挨在一起。

**解法**：冲突块里"我方 16 行回填 + 上游 17 行新方法"都要，只丢掉我方侧最后两行（`async def _init_community_backends(...)` 的函数头和 docstring，上游已删除该函数，冲突块之后的函数体属于 `_seed_builtin_source`）。解完后应为：

```python
                    logger.info(
                        f"Updated backend_source for existing built-in backend {backend.backend_name}"
                    )

            # Built-in definitions can gain UI parameter hints (common_parameters)
            # in upgrades, but seeding above is create-only, so pre-existing rows
            # would never receive them. Fill in ONLY when the row has none — an
            # operator-customized list is never clobbered by startup.
            if (
                backend
                and not backend.common_parameters
                and built_in_backend.common_parameters
            ):
                await backend.update(
                    session,
                    {"common_parameters": built_in_backend.common_parameters},
                )
                logger.info(
                    f"Backfilled common_parameters for built-in backend {backend.backend_name}"
                )

    async def _reconcile(self):
        try:
            async with async_session() as session:
                await gather_and_merge(
                    session, InferenceBackendSource, reconcile_backend
                )
        except Exception as e:
            logger.error(f"Failed to reconcile community backends: {e}")

    async def _seed_builtin_source(self):
        """Upsert the BUILTIN InferenceBackendSource from the packaged
        ...（上游 docstring 与函数体原样）
```

注意缩进：回填的 `if` 与上面的 `if not backend: / elif backend.backend_source is None:` **同级**（在 `for` 循环体内，12 个空格），不是嵌在 `elif` 里面。

自检：
```bash
grep -c "_init_community_backends" gpustack/server/controllers.py          # 期望 0
grep -c "Backfilled common_parameters" gpustack/server/controllers.py      # 期望 1
grep -n "await self._init_built_in_backends(session)" -A1 gpustack/server/controllers.py   # 下一行应是 await self._seed_builtin_source()
```

需要新增的测试（现在没有覆盖这段回填）：`tests/server/test_builtin_backend_backfill.py`，三个用例：
1. 库里已有内置后端行且 `common_parameters` 为空，内置定义里有值 → 启动后被回填。
2. 库里该行 `common_parameters` 已有运维自定义值 → 启动后**不变**。
3. 库里没有该行 → 新建，值等于内置定义。

### C3 `gpustack/server/server.py`（2 处，两边都要）

**冲突 1（import，约第 104 行）**：三行都要，顺序如下：

```python
from gpustack.server.worker_syncer import WorkerSyncer
from gpustack.server.video_task_sweeper import VideoTaskSweeper
from gpustack.server.video_storage_janitor import VideoStorageJanitor
from gpustack.server.scaling_scheduler import ScalingScheduler
```

**冲突 2（`_start_leader_tasks()` 内，约第 1745 行）**：四个启动调用都要，我方在前、上游在后：

```python
        # Worker Syncer (checks worker reachability and updates states)
        self._start_worker_syncer(self._app)

        # Video task sweeper (LightX2V death-requeue + re-dispatch)
        self._start_video_task_sweeper(self._app)

        # Video storage janitor (LightX2V NFS TTL + watermark eviction)
        self._start_video_storage_janitor()

        # Scaling Scheduler (drives model replicas on a cron timetable)
        self._start_scaling_scheduler()

        # Operator Settings (converges the operator settings GPU Service
        # clusters are configured with)
        self._start_gpustack_operator_settings()
```

四个任务都通过同一个 `self._create_async_task(...)` 注册，都只在 leader 上跑（`_start_leader_tasks`），机制一致，没有生命周期上的额外工作。

**同文件里自动合并、但必须确认还在的我方代码**（Git 没报冲突，逐项 grep）：

| 我方代码 | 位置 | 检查命令（期望命中数） |
|---|---|---|
| `await self._apply_persisted_config()` 紧跟在 `await self._prepare_data()` 之后 | `start()` | `grep -n "_prepare_data()" -A1 gpustack/server/server.py`（下一行是它） |
| `self._probe_lightx2v_nfs()` 调用 | `start()` | `grep -c "self._probe_lightx2v_nfs()" …`（1） |
| `_apply_persisted_config` / `_probe_lightx2v_nfs` / `_start_video_task_sweeper` / `_start_video_storage_janitor` 四个方法定义 | 类内 | `grep -c "def _apply_persisted_config\|def _probe_lightx2v_nfs\|def _start_video_task_sweeper\|def _start_video_storage_janitor" …`（4） |
| `from gpustack.config.config import Config, set_global_config` | 文件头 | `grep -c "set_global_config" …`（≥2） |

相关文件 `gpustack/utils/config.py`、`gpustack/routes/config.py` 上游只加了两个 Grafana 字段和一个 `timezone` 返回值，与我方的运行时配置落库互不影响，自动合并结果直接用。

### C4 `gpustack/worker/serve_manager.py`（7 处冲突 + 6 处"自动合并但要手工处理"）

这是本次升级里唯一需要判断的文件。**原则：以上游 v2.3.0 为主体**，我方 10-18 的两个修复（`521d3c6b` 多机补端口/重拉从节点、`8b02caf3` 启动防重入）逐条对照上游，只移植上游没有等价实现的那一条。

先说结论，我方 5 个行为在上游的对应情况（逐段读过上游 v2.3.0 源码）：

| # | 我方行为（提交） | 上游 v2.3.0 是否已有 | 处置 |
|---|---|---|---|
| 1 | 启动防重入 `_start_guard`（8b02caf3） | **有**：`_starting_model_instances` + `_model_instance_start_lock`，外加 `@_model_lifecycle` 每实例锁 | 删我方，用上游 |
| 2 | 启动前先回写一次 `port/ports`（521d3c6b） | 不需要：上游在子进程启动后与 `INITIALIZING` 一起回写。我方这次提前回写正是 #1 那个回声的来源 | 删我方 |
| 3 | 主进程组重建后重拉从节点 `_follower_launch_is_stale` + `main_launch_pid`（521d3c6b） | **没有**。上游 `_start_or_restart_subordinate()` 看到从节点容器状态正常就直接返回 | **移植**进上游新函数 |
| 4 | `ports` 过短时重新分配 `_ports_already_reserved` / `_required_port_count`（521d3c6b） | **没有**。上游 `_assign_local_ports()` 对已有 `mi.port` 的实例原样沿用端口 | 不移植（与上游跨 worker 端口占位机制冲突）；保留 `vllm.py` 里的长度检查作为明确报错；列入必测 |
| 5 | 启动期从节点出错不把整个实例标 ERROR（521d3c6b 的三处小改） | **部分有**：上游按 `restart_on_error` 决定是否上抛，并新增了从节点恢复逻辑 | 默认用上游；列入必测，不通过按本节末尾"回退补丁"处理 |

#### 7 处冲突的解法

| 冲突 | 位置（合并文件行号，约） | 解法 |
|---|---|---|
| 1 | `__init__`，L441 | 取上游两行（`self._log_persistence = {}`、`self._log_persistence_lock = threading.Lock()`）。**不要** `self._start_guard` |
| 2 | `sync_model_instances_state`，L937 | 取上游（`if sw.state == RUNNING:` + 两行 `pop`）。我方的 `and not sw.state_message` 不要 |
| 3 | `_dispatch_model_instance_event`，L1562 | 取上游一行 `self._start_or_restart_subordinate(mi)`；我方逻辑按下面"移植 #3"加进该函数 |
| 4 | `_launch_model_instance` 开头，L2123 | 取上游 7 行（恢复 `_model_instance_by_instance_id`、`_align_instance_log_dir`）。**不要** `_start_guard.add/discard` 和 `_start_model_instance_body` |
| 5、6、7 | `_assign_ports` 一带，L2358–2550 | 三处全取上游（`_assign_ports` / `_assign_local_ports` / `_assign_distributed_ports` / `_select_ports` 等） |

#### 6 处自动合并残留（Git 没报冲突，但我方代码已混进上游新逻辑，必须手工处理）

| 残留 | 位置（约） | 现状 | 处置 |
|---|---|---|---|
| A | `_dispatch_model_instance_event` 的 SCHEDULED 分支，L1540–1556 | `if mi.id in self._start_guard or self._is_provisioning(mi): … else: self._restart_model_instance(mi)` | 还原成上游：直接 `self._restart_model_instance(mi)` + `logger.trace(...)`，并删掉我方加的 4 行注释 |
| B | `_launch_model_instance` 第一句，L2119 | `if mi.id in self._start_guard or self._is_provisioning(mi):` | 还原成上游 `if self._is_provisioning(mi):` |
| C | `_launch_model_instance` 内，L2176–2185 | `# Publish the reserved list before the child watches for STARTING` 及其 `self._update_model_instance(mi.id, port=…, ports=…)` | **整段删除** |
| D | `_launch_model_instance` 从节点 patch，L2228–2236 | 我方删掉了上游"`INITIALIZE_LATER` 直接置 RUNNING"的 5 行 | 恢复上游那 5 行，同时**保留**我方的 `sw.state_message = ""` 和 `sw.main_launch_pid = mi.pid`（见下方代码） |
| E | `sync_model_instances_state`，L759–766 | `if not workload and not is_main_worker and self._follower_startup_pending(model_instance): continue` | **保留但收窄**：把条件里的 `_follower_startup_pending` 换成 `_follower_launch_is_stale`（见下方"残留 E"）。原样保留会让上游测试 `test_subordinate_health_check_waits_for_start[starting-INITIALIZE_LATER]` 失败——上游明确要求"实例已 STARTING、从节点没有容器"要上报 |
| F | `_get_main_worker_distributed_state`，L1055–1058 | `if model_instance.state in _FOLLOWER_STARTUP_STATES: return {"should_update": False}` | **删除**，还原成上游。保留它会让上游"`restart_on_error=False` 时把从节点的终态失败上抛"的逻辑（L690–710）在启动期永远不触发，实例会一直卡在 starting |
| — | L2288–2336 | `_required_port_count()`、`_ports_already_reserved()` 两个方法 | 删除（冲突 5–7 取上游后已无调用方） |
| — | 文件头 | `_port_lock`（如还在）、`Set` 等只被已删代码用到的名字 | `make lint` 报未使用就删 |

残留 D 解完后的代码：

```python
            # Get patch dict for subordinate worker.
            else:
                sw.state = ModelInstanceStateEnum.INITIALIZING
                # For initialize later mode, the state is set to RUNNING directly,
                # which means the subordinate worker doesn't need to wait for the main worker to be healthy.
                if (
                    mi.distributed_servers.mode
                    == DistributedServerCoordinateModeEnum.INITIALIZE_LATER
                ):
                    sw.state = ModelInstanceStateEnum.RUNNING
                sw.state_message = ""
                sw.pid = process.pid
                # fork: remember which main process group this follower joined.
                sw.main_launch_pid = mi.pid
                patch_dict = {
                    f"distributed_servers.subordinate_workers.{sw_pos}": sw,
                }
```

残留 E 解完后的代码（放在上游 `workload = get_workload(workload_name)` 之后、`if workload and workload.state in [PENDING, INITIALIZING]` 之前）：

```python
            # fork: the instance turns STARTING before this follower receives the
            # event and creates its container. Until it has launched against the
            # current main process group, a missing container is not a failure.
            if (
                not workload
                and not is_main_worker
                and self._follower_launch_is_stale(model_instance)
            ):
                continue
```

为什么这样收窄：`_follower_launch_is_stale(mi)` 为真 ⇔ `INITIALIZE_LATER` 且实例在启动期 且 `mi.pid` 非空 且 本从节点记录的 `main_launch_pid != mi.pid`。配合"没有容器"，含义就是**本从节点还没针对当前主进程组启动过**，这才是我方原修复要保护的窗口（主节点刚转 `STARTING`、后面的从节点还在等前一个从节点 `RUNNING`）。从节点已针对当前 `mi.pid` 启动过、之后容器消失的，走上游报错路径；`mi.pid` 为空时不豁免（上游行为）。试合并实测：上游 203 个测试 + 我方 5 个测试全部通过。

#### 移植 #3：把"主进程组已重建"判断加进 `_start_or_restart_subordinate()`

保留我方的 `_FOLLOWER_STARTUP_STATES`、`_follower_startup_pending()`、`_follower_launch_is_stale()`（自动合并后都还在，不用动），以及 `gpustack/schemas/models.py` 里 `ModelInstanceSubordinateWorker.main_launch_pid` 字段（它在 JSON 列里，不需要数据库迁移）。

上游函数改成（`# fork:` 标出的 3 处是新增/改动，其余是上游原文）：

```python
    def _start_or_restart_subordinate(self, mi: ModelInstance) -> None:
        """Start a follower, respecting the deployment's recovery policy."""
        deployment_metadata = mi.get_deployment_metadata(self._worker_id)
        workload_name = deployment_metadata.name if deployment_metadata else mi.name
        workload = get_workload(workload_name)
        # fork: a container that joined a previous main process group never
        # joins the new one (TCP store waits for the missing rank forever).
        stale = workload is not None and self._follower_launch_is_stale(mi)
        if (
            workload
            and not stale  # fork
            and workload.state
            not in (
                WorkloadStatusStateEnum.UNKNOWN,
                WorkloadStatusStateEnum.UNHEALTHY,
                WorkloadStatusStateEnum.INACTIVE,
                WorkloadStatusStateEnum.FAILED,
            )
        ):
            return

        subordinate = next(
            worker
            for worker in mi.distributed_servers.subordinate_workers
            if worker.worker_id == self._worker_id
        )
        initial_start = not workload and subordinate.state in (
            ModelInstanceStateEnum.PENDING,
            ModelInstanceStateEnum.SCHEDULED,
            ModelInstanceStateEnum.DOWNLOADING,
        )
        # The parent can still be STARTING after this follower fails. Its
        # local state distinguishes first launch from recovery on later events.
        # fork: a stale follower is replaced regardless of the recovery policy;
        # it is part of the main's (re)start, not a recovery of its own.
        if (
            not stale
            and not initial_start
            and not self._auto_restart_enabled(mi, use_cache=not bool(mi.group_id))
        ):
            logger.debug(...)   # 上游原文
            return

        if workload:
            self._stop_model_instance(mi, clear_restart_backoff=False)
        self._start_model_instance(mi)
        if stale:
            logger.info(
                f"Restarted subordinate workload {workload_name} for {mi.name}: "
                "main process group changed."
            )
        else:
            logger.trace(
                f"UPDATED event: started model instance {mi.name} on subordinate worker."
            )
```

#### 自检（合并后在仓库根目录执行）

```bash
f=gpustack/worker/serve_manager.py
grep -c "_start_guard\|_start_model_instance_body\|_ports_already_reserved\|_required_port_count" $f   # 期望 0
grep -c "Publish the reserved list before the child" $f                                              # 期望 0
grep -c "_follower_launch_is_stale" $f            # 期望 3（定义 + sync 里 1 处 + _start_or_restart_subordinate 里 1 处）
grep -c "_follower_startup_pending" $f            # 期望 2（定义 + _follower_launch_is_stale 里 1 处）
grep -c "sw.main_launch_pid = mi.pid" $f          # 期望 1
grep -c "DistributedPortClient\|_assign_distributed_ports\|_starting_model_instances" $f   # 试合并实测 11（上游机制都在）
grep -n "_FOLLOWER_STARTUP_STATES" $f             # 只应出现在常量定义和 _follower_startup_pending 里（共 2 处）
grep -c "len(ports) < 2\|len(ports) < 3" gpustack/worker/backends/vllm.py   # 期望 2（我方端口长度检查还在）
grep -c "reserved_ports" gpustack/worker/backends/base.py                    # 期望 ≥3（我方 STARTING 端口保留还在）
diff <(git show v2.3.0:$f) $f | grep -c "^[<>]"   # 试合并实测为 90；记录你的数字写进自测报告，明显更多说明混进了不该留的我方代码
```

#### 回退补丁（只有 §8.4 的 M 组不通过时才用，用之前先在报告里写明哪个场景、什么现象）

- 场景 M2/M3 里从节点起不齐、且日志显示后一个从节点在等前一个从节点 RUNNING：把残留 D 改回我方写法（去掉"直接置 RUNNING"那 5 行），并把冲突 2 改回带 `and not sw.state_message` 的版本。
- 场景 M4 里一个从节点启动失败导致整组反复重启不收敛：恢复残留 F。
- 场景 M5（端口列表过短）如果在现网是会实际遇到的操作路径：不在本次升级里补，报告给验收方单独立项。

### D2 数据库迁移链（Git 不报冲突，不改 server 起不来；改错会跳过上游迁移）

**现状**：我方 4 个迁移和上游 v2.3.0 的 9 个迁移都从 `c4d7e8f9a0b1` 分出去。

```
c4d7e8f9a0b1 ─┬─ e1f2a3b4c5d6 → f3a1b2c4d5e7 → a7c3e5d19b42 → b8d4f6a2c1e9            （我方：视频任务表、进度、runtime_configs、assigned_at）
              └─ 367a3982fcde → a3f5c1d9e0b2 → d5e8f0a1b2c3 → b7e2c4d15a80 → e4a1c8b7d0f3
                 → f1a2b3c4d5e6 → b4c5d6e7f8a9 → f4a5b6c7d8e9 → a7e2c9d4f681            （上游 v2.3.0）
```

我按 `down_revision` 把三种做法的迁移图算了一遍（脚本算图，不是在真库上跑）：

| 做法 | head | 现网库（当前版本号 `b8d4f6a2c1e9`）升级时会执行 |
|---|---|---|
| 直接合并，什么都不改 | 两个：`a7e2c9d4f681`、`b8d4f6a2c1e9` | server 启动时 `command.upgrade(cfg, "head")` 因多 head 报错 |
| 沿用 `pack/Dockerfile.acr` 现在的 `sed`（把 `e1f2a3b4c5d6` 的父版本改写成基础镜像的 head） | 一个：`b8d4f6a2c1e9` | **0 个**。上游 9 个迁移全部被跳过，库结构停在 v2.2.3，server 带着缺表缺列启动 |
| 新增一个双父 merge revision（本方案） | 一个：`d9f0a1b2c3e4` | 10 个：上游 9 个 + merge 本身 |

上游 9 个迁移没有任何一个涉及 `operationenum`、视频任务表或 `runtime_configs`（已 grep），两条链改的内容不相交，所以 merge revision 的 `upgrade()` 可以是空的。

**改动 1：新增文件** `gpustack/migrations/versions/2026_11_10_1200-d9f0a1b2c3e4_merge_fork_and_upstream_v2_3_0.py`

```python
"""merge fork (video tasks / runtime configs) and upstream v2.3.0 heads

Revision ID: d9f0a1b2c3e4
Revises: b8d4f6a2c1e9, a7e2c9d4f681
Create Date: 2026-11-10 12:00:00.000000

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'd9f0a1b2c3e4'
down_revision: Union[str, Sequence[str], None] = ('b8d4f6a2c1e9', 'a7e2c9d4f681')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Merge point only. The two branches touch disjoint tables.
    pass


def downgrade() -> None:
    pass
```

`d9f0a1b2c3e4` 已确认在 `upstream/main` 和 `v2.3.1rc1` 里没有被占用。**不要修改我方已有 4 个迁移文件的任何内容**（包括 `down_revision`）：现网库已经记录了这些版本号。

**改动 2：`pack/Dockerfile.acr`**（L115–135 那段"Migration chain fix-up"）
- 删除 `sed -i "s/^down_revision: …"` 那一行，以及只为它服务的 `MIG_FILE=` 和 `grep -n "^down_revision"` 两行，并把上面的注释改成说明现在用 merge revision。
- 保留"读取基础镜像 head"的 `BASE_HEAD=` 那一行，但把它变成断言：基础镜像的 head 必须等于 `a7e2c9d4f681`，否则构建失败（防止以后有人换了基础镜像却没加新的 merge revision）：
  ```dockerfile
  test "${BASE_HEAD}" = "a7e2c9d4f681" || { echo "base image alembic head ${BASE_HEAD} != a7e2c9d4f681: add a new merge revision before changing BASE_IMAGE"; exit 1; }
  ```
- 把最终 head 断言从 `b8d4f6a2c1e9` 改成 `d9f0a1b2c3e4`，并要求 `get_heads()` 恰好 1 个。
- 在迁移文件的 `COPY` 列表（L81–90）和 `py_compile` 列表（L164–167）里各加上新的 merge revision 文件。

**改动 3：新增测试** `tests/migrations/test_single_head.py`（防止以后再出现双 head）

```python
from alembic.config import Config
from alembic.script import ScriptDirectory
import gpustack, os

def _script():
    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(gpustack.__file__), "migrations")
    )
    return ScriptDirectory.from_config(cfg)

def test_single_alembic_head():
    assert _script().get_heads() == ["d9f0a1b2c3e4"]

def test_fork_and_upstream_branches_are_both_ancestors():
    revs = {r.revision for r in _script().iterate_revisions("d9f0a1b2c3e4", "base")}
    assert {"b8d4f6a2c1e9", "a7e2c9d4f681", "e1f2a3b4c5d6", "367a3982fcde"} <= revs

def test_fork_chain_root_is_untouched():
    # 现网库依赖这个父版本；任何构建步骤都不许改写它
    assert _script().get_revision("e1f2a3b4c5d6").down_revision == "c4d7e8f9a0b1"
```

**改动 4：我方 3 个测试文件的 fixture**（不改会从通过变成 25 个 ERROR，试合并已实测）

`tests/routes/test_least_pending_tie_break.py`、`tests/routes/test_runtime_config_persistence.py`、`tests/routes/test_video_queue_position.py` 各有一个 fixture：建空 SQLite → `stamp` 到 `c4d7e8f9a0b1` → `upgrade(cfg, "head")`，目的是只跑我方 4 个迁移。有了 merge revision 后 `head` 会把上游 9 个迁移也带上，而空库里没有上游迁移要改的表，报 `sqlalchemy.exc.NoSuchTableError: models`。

三处都把目标从 `"head"` 改成我方链的末端：

```python
    alembic_command.stamp(cfg, PREVIOUS_REVISION)
    alembic_command.upgrade(cfg, "b8d4f6a2c1e9")   # fork branch only; "head" would also run upstream's
```

改后这 3 个文件实测 31 passed。全链路（上游 9 个 + 我方 4 个 + merge）的验证不靠这些 fixture，靠 §8 的 D 组在真 PostgreSQL 上做。

**以后每次升级上游都照此办理**：上游新 head 出来后，再加一个 merge revision，父版本是（我方当前 head，上游新 head）。例如升 v2.3.1 时父版本是 `('d9f0a1b2c3e4', 'c8d3e6f1a902')`——前提是那时我方没有再加自己的迁移；如果加了，就用我方最新的那个。

### D3 Overlay 发布链（`pack/Dockerfile.acr` 等 4 个文件）

Overlay 镜像是"上游发布镜像 + 整文件覆盖我方改过的模块"。基础镜像必须和代码合并到的上游版本一致，否则我方文件会盖掉上游 v2.3.0 的新逻辑或缺少它依赖的模块。

**前置条件（开发 agent 做不了就停下来上报）**：ACR 里要有 `crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:v2.3.0`（上游 `gpustack/gpustack:v2.3.0` 的多架构镜像原样同步过去）。检查命令：
```bash
docker buildx imagetools inspect crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:v2.3.0
```
期望输出里同时有 `linux/amd64` 和 `linux/arm64`。

**要改的固定值（4 处，全部从 `v2.2.3` 改成 `v2.3.0`）**

| 文件 | 位置 |
|---|---|
| `hack/upstream-base.env` | `UPSTREAM_BASE=v2.3.0` |
| `pack/Dockerfile.acr` | L17 注释里的示例命令、L31 `ARG BASE_IMAGE=…:v2.3.0` |
| `.github/workflows/pack-acr-overlay.yml` | L24 `base_image` 的 `default` |
| `pack/Dockerfile.acr` 迁移段 | 见 D2 的改动 2 |

**COPY 清单**：我在试合并结果上按 `hack/check-overlay-list.sh` 的同一口径算过（`git diff --name-only --diff-filter=ACMRT v2.3.0 -- gpustack static`）：解完冲突后我方相对 v2.3.0 改动的文件仍是现在清单里的那 49 个，没有需要去掉的，也没有相对 v2.3.0 删除的文件。**只需新增 1 行**：D2 的 merge revision 文件。最终 50 行。

如果你新增了任何 `gpustack/` 下的文件，也要加进清单。`tests/` 下的文件不进清单。

`py_compile` 列表同步加上 merge revision 文件。

**UI 部分**：workflow 里 `ui_ref` 默认是 `main`。UI 升级分支合进 `reputationly/gpustack-ui` 的 `main` 之前，构建后端升级分支的镜像时必须显式传 `ui_ref=<UI 升级分支名>`，否则会把 v2.2.3 的旧 UI 盖到 v2.3.0 的后端上。另外确认 UI 上游的 Node / pnpm 版本要求没有变（见 §E）；变了就同步改 workflow 里的 `node-version: 22` 和 `pnpm@9.3.0`。

完整改法见 `ref-patches/overlay-files.vs-origin-main.diff`（试合并上已改好，`hack/check-overlay-list.sh` 输出 OK / 50 个文件）。

**自检**
```bash
hack/check-overlay-list.sh                    # 期望：OK: overlay COPY list matches the 50 files changed vs v2.3.0.
grep -c "v2.2.3" hack/upstream-base.env pack/Dockerfile.acr .github/workflows/pack-acr-overlay.yml   # 期望 0 / 3 / 0（Dockerfile.acr 剩的 3 处是 COPY 清单里的历史说明注释，不改）
grep -c "sed -i" pack/Dockerfile.acr          # 期望 0
```

### D4 我方内部文档搬出 `docs/`（Git 不报冲突，但 57 个上游测试会失败）

**现状**：我方在 `docs/` 下放了 21 个内部文件（18 个设计 / 运维 `.md`、`docs/configs/wan_t2v_int8_4card.json`、`docs/scripts/lx2v-fleet.sh`、`docs/scripts/lx2v-node.sh`），并在 `mkdocs.yml` 的 `nav` 里加了一组 `Secondary Development`。

**上游 v2.3.0 的变化**：`docs/` 成了产品文档的唯一来源，带 4 种语言的译文（`docs-i18n/{ja,ru,tr,zh}/`），并且：
- `tests/hack/test_five_language_sources.py`、`tests/hack/test_japanese_doc_sources.py` 要求 `docs/` 下**每个** `.md` 在 `docs-i18n/ja|ru|tr/` 有同名译文。我方 18 个 `.md` 没有 → 57 个测试失败（试合并实测：38 + 19）。
- full image 构建会把 `docs/` 编译进镜像并由 server 对外提供。内部文档（现网部署现状、节点运维手册等）不应该出现在产品文档站里。

**解法：把这 21 个文件整体搬到仓库根目录的 `docs-fork/`，`mkdocs.yml` 恢复成上游原样。**

```bash
# 1. 搬文件（列表 = 我方相对 v2.3.0 在 docs/ 下新增的全部文件；应为 21 个）
git -c core.quotepath=off diff --name-only --diff-filter=A v2.3.0 -- docs > /tmp/forkdocs.txt
wc -l < /tmp/forkdocs.txt
while IFS= read -r f; do d="docs-fork/${f#docs/}"; mkdir -p "$(dirname "$d")"; git mv "$f" "$d"; done < /tmp/forkdocs.txt

# 2. mkdocs.yml 取上游（我方只加了 nav 里那 3 行）
git checkout v2.3.0 -- mkdocs.yml

# 3. 代码注释和文档互相引用的路径同步改名（迁移文件不动，见下）
git -c core.quotepath=off grep -ln "docs/\(lightx2v\|acestep\|视频\|现网\|自部署\|secondary-dev\|minimax\|cluster-restore\|cu130\|新引擎\|200卡\|configs/wan\|scripts/lx2v\)" \
  -- gpustack tests AGENTS.md pack hack .github docs-fork | grep -v "migrations/versions" > /tmp/docref_files.txt
xargs -d '\n' sed -i -E 's#(^|[^A-Za-z0-9_-])docs/(lightx2v|acestep|视频|现网|自部署|secondary-dev|minimax|cluster-restore|cu130|新引擎|200卡|configs/wan|scripts/lx2v)#\1docs-fork/\2#g' < /tmp/docref_files.txt
```

第 3 步在试合并上改了 15 个文件，全部是注释 / 文档字符串 / 文档正文里的路径，没有任何可执行代码。`gpustack/migrations/versions/` 下两个迁移文件的注释里也有旧路径，**不要改**（D2 规定我方已有迁移文件一个字节都不动）。

自检：
```bash
git diff --name-only --diff-filter=A v2.3.0 -- docs | wc -l        # 期望 0
git diff v2.3.0 -- mkdocs.yml mkdocs.offline.yml | wc -l            # 期望 0
ls docs-fork | wc -l                                                 # 期望 20（18 个 .md + configs + scripts）
uv run pytest tests/hack -q                                          # 试合并实测 616 passed
hack/check-overlay-list.sh                                           # 仍然 OK、50 个文件（第 3 步只改了已在清单里的文件）
```

以后新增内部文档一律放 `docs-fork/`，在 `AGENTS.md` 的 Fork notes 里写一句。

## 6. 已知限制与注意事项

1. **自研后端 + `gpu_type_selector` 不受支持。** 上游 `build_candidate_selector()` 里 `if model.gpu_type_selector:` 分支排在自研后端分支之前，命中后走上游的通用选择器，绕过我方的整卡 / 显存下限逻辑。本次不改顺序（改了会影响上游的 instance type 功能）。自测 S6 要确认现网没有这种组合；有就上报。
2. **`ports` 过短不再自动重新分配。** 我方旧的 `_ports_already_reserved()` / `_required_port_count()` 被删除（它绕过上游的跨 worker 端口占位）。升级前就存在、`ports` 长度不足的多机实例，升级后需要删掉重建，不会自愈。`vllm.py` 里的长度检查保留，会给出明确报错而不是静默用错端口。
3. **overlay 不改版本号。** overlay 镜像的 `/version` 仍显示基础镜像的 `v2.3.0` / `63a03ba`（试合并实测）。这是现有行为，本次不改；判断部署的是不是我方构建，看镜像 tag 或 `GET /v2/inference-backends` 里有没有自研后端。
4. **`/v1/videos` 路由归属。** 上游 v2.3.0 新增了网关侧的 `/v1/videos/sync`。我方的 `/v1/videos` 异步 facade 继续由 GPUStack server 处理。两者路径不同，试合并上 server 能正常注册两套路由，但没有在开着网关的环境里实际发过请求，V8 要验证。
5. **前端测试必须用 Node 22。** Node 24 下上游自己的测试有 8 个失败（纯上游 `v2.3.0` 也一样），Node 22 下合并前后都是 0 失败。另外 `tsc --noEmit` 在合并后的 UI 上报 39 个类型错误，没有一个落在我方相对 `v2.3.0` 改过的 42 个文件里；它不在上游的 `pnpm test` / build 流程中，不作为验收项。
6. **试合并验证过的和没验证过的**，见附录 B。没验证过的部分（真实多机、现网库、ACR 双架构构建、UI 手测）正是 §8 里标"需环境"的项，是这次升级风险所在，不要跳过。

## 7. 前端 `reputationly/gpustack-ui`

前端是独立仓库，必须和后端同一轮升到 **UI tag `v2.3.0`（`7a710563`）**。我方 UI = UI tag `v2.2.3`（`29b965eb`）+ 26 个提交 / 44 个文件；上游 v2.3.0 领先 376 个提交。

```bash
git clone https://github.com/reputationly/gpustack-ui && cd gpustack-ui
git remote add upstream https://github.com/gpustack/gpustack-ui.git
git fetch upstream --tags
git checkout -b chore/upstream-v2.3.0 origin/main
git merge --no-ff v2.3.0        # 5 个冲突文件
```

### 7.1 五个冲突文件的解法（已在试合并上逐个做过并构建通过）

| 文件 | 解法 |
|---|---|
| `package.json` | **整文件与上游一致**。冲突块取上游（`"test": "node scripts/test.mjs"`，不要我方的 `vitest run` / `test:watch`）；再把自动合并留下的我方两行 devDependencies 删掉：`"vite": "^7",` 和 `"vitest": "^4.1.10",`。自检：`git diff v2.3.0 -- package.json \| wc -l` = 0 |
| `pnpm-lock.yaml` | `git checkout v2.3.0 -- pnpm-lock.yaml`。不要手工拼接，也不要重新生成。自检：`git diff v2.3.0 -- pnpm-lock.yaml \| wc -l` = 0，且 `pnpm install --frozen-lockfile` 成功 |
| `src/config/settings.ts` | `PaginationKey` 末尾四个 key 都保留，顺序：`Instances`、`VideoTasks`、`InstanceTypes`、`CacheServices` |
| `src/locales/ja-JP/menu.ts` | 冲突块取**上游**整段（上游把这一段译成了日文并新增 `menu.models.kvCache`、`menu.models.kvCacheDetail`、`menu.gpuService.instanceTypes`），在它前面插入我方两行：`'menu.resources.videoTasks': '生成タスク',`、`'menu.resources.storageSettings': '動画ストレージ',` |
| `src/locales/ru-RU/menu.ts` | 同上：取上游（修正了 `Файлы моделей` 拼写），前面插入我方两行：`'menu.resources.videoTasks': 'Задачи генерации',`、`'menu.resources.storageSettings': 'Хранилище видео',` |

`settings.ts` 解完后：

```ts
  Credentials: 'Credentials',
  Instances: 'Instances',
  VideoTasks: 'VideoTasks',
  InstanceTypes: 'InstanceTypes',
  CacheServices: 'CacheServices'
};
```

参考补丁：`ref-patches/ui-conflicts.vs-ui-v2.3.0.diff`。

### 7.2 Git 不报冲突但必须改：`src/pages/playground/video/task-inputs.test.ts`

上游 v2.3.0 新增了 `pnpm test`（`scripts/test.mjs`，用 `node --import tsx --test` 自动发现 `src/`、`scripts/` 下所有 `*.test.ts`），并且 CI 会跑。我方这个测试文件用的是 Vitest API，会被上游 runner 捡到并失败。

把它改写成 `node:test` + `node:assert/strict`，测试内容和用例数量不变：

- 文件头：`import { describe, expect, it } from 'vitest';` → `import assert from 'node:assert/strict';` + `import { describe, it } from 'node:test';`
- `expect(x).toBe(y)` → `assert.equal(x, y)`；`expect(x).toBeNull()` → `assert.equal(x, null)`；`expect(x).toEqual(y)` → `assert.deepEqual(x, y)`
- `toMatchObject` → 文件内加一个小助手（不要用 `assert.partialDeepStrictEqual`，Node 22 早期版本没有）：

```ts
// Subset match: every key in `want` must be deeply equal in `got`.
const assertMatches = (got: unknown, want: Record<string, unknown>) => {
  assert.ok(got && typeof got === 'object');
  for (const [key, value] of Object.entries(want)) {
    assert.deepEqual((got as Record<string, unknown>)[key], value);
  }
};
```

- `it.each([...])(name, fn)`（3 处）→ `for (const [...] of [...]) { it(\`...\`, () => {...}); }`

完整改写见 `ref-patches/ui-task-inputs-test.vs-fork-main.diff`，可以直接 `git apply`。改完后文件里不应再出现 `expect(`、`it.each`、`vitest`。

### 7.3 自动合并但要确认还在的我方改动

```bash
grep -n "COS_ENABLED\|DISPATCH_ENABLED" .github/workflows/ci.yml          # 两个守卫都应还在（fork 没有 COS / dispatch 密钥）
grep -rn "videoTasks\|storageSettings" src/locales/*/menu.ts | wc -l        # 每种语言 2 行
grep -n "VideoTasks" src/config/settings.ts; grep -n "video-tasks\|videoTasks" config/routes.ts   # 分页 key 和路由都在
git diff v2.3.0 --stat | tail -1                                            # 试合并实测：42 files changed, 2505 insertions(+), 48 deletions(-)
```

### 7.4 UI 自测（Node 22，见 `.nvmrc`；pnpm 9.3.0，见 `package.json` 的 `packageManager`）

| 编号 | 命令 | 通过标准 |
|---|---|---|
| U1 | `corepack pnpm@9.3.0 install --frozen-lockfile` | 退出码 0，lock 无改动 |
| U2 | `pnpm run check:locales` | 输出 `✅ All keys are consistent!` |
| U3 | `pnpm test`（必须在 Node 22 下） | 退出码 0，`# fail 0`。试合并实测（Node v22.23.4）：两段汇总分别是 181 / 434 个全部通过，含我方的 `inferVideoTaskType`、`validateVideoInputs` 两组 |
| U4 | `scripts/build`（即 `pnpm run build`） | 退出码 0，生成 `dist/index.html` |
| U5 | 浏览器手测（接升级后的后端） | 见 §8 的 V 组和 N 组 |

U3 说明：不要用 Node 24 跑。Node v24.22.2 下纯上游 `v2.3.0` 和合并后都有同样的 8 个失败（`scripts/test` 自测和 ja-JP / ru-RU 的 history / rollback 标题），换成 Node 22 后两边都是 0 失败，所以那是 Node 版本问题，与合并无关。

### 7.5 UI 与后端的衔接

- UI 分支合入后打包产物（`dist`）由后端的 full image 构建和 `pack-acr-overlay` 工作流取用。overlay 工作流会检出 `gpustack-ui` 到 `gpustack-ui-src/`；确认它检出的是升级后的 UI 提交，而不是旧的 `main`。
- 后端与 UI 必须成对发布。旧 UI + 新后端：上游新页面（KV cache、instance types、revision history）缺失；新 UI + 旧后端：新页面请求 404。

## 8. 自测要求

每一项都要在自测报告里贴**命令 + 原始输出**（长输出贴首尾并附完整日志文件）。标 `必过` 的项不通过就不能提交验收；标 `需环境` 的项如果你没有对应环境，写明"未执行 + 原因"，不要写成通过。

"试合并实测"是我在草稿 worktree 上按本文档解法跑出来的数字，供你对照；你的数字明显不同就先查原因。

### 8.1 G 组：静态与单元测试（必过）

| 编号 | 命令 | 通过标准 | 试合并实测 |
|---|---|---|---|
| G1 | `git diff --check && git grep -n "^<<<<<<< \|^>>>>>>> " -- . ':!docs' \| wc -l` | 无输出 / 0 | — |
| G2 | `uv lock --check && uv sync` | 退出码 0；`git diff v2.3.0 -- uv.lock pyproject.toml \| wc -l` = 0 | 通过 |
| G3 | `uv run python -m compileall -q gpustack && uv run python -c "import gpustack"` | 退出码 0 | 通过 |
| G4 | `make install`（下载 UI 资源和 chart 包；`tests/k8s` 依赖它） | 生成 `gpustack/ui/static/charts/gpustack-chart.tgz` | 未执行 |
| G5 | `make lint` | 退出码 0 | 未整体执行；单文件 black 通过 |
| G6 | `make test` | 0 failed | 见下 |
| G7 | `make generate && git status --short gpustack/client` | 无改动（有改动就提交生成结果，不手改） | 未执行 |
| G8 | 各节的"自检"命令（§5 每个文件末尾） | 全部符合期望值 | 通过 |

G6 的对照数据。试合并上没做 G4 时的全量结果是 `98 failed, 9680 passed, 72 skipped`，失败只落在 4 个文件：

| 文件 | 失败数 | 原因 | 按本文档处理后 |
|---|---|---|---|
| `tests/k8s/test_bootstrap_manifest.py` | 23 | 环境：缺 `gpustack-chart.tgz`（没跑 G4） | 做了 G4 应通过；仍失败则在纯 `v2.3.0` 检出上对照并上报 |
| `tests/hack/test_docs_packaging.py` | 18 | `pack.yaml` 取了我方的 `on:` | 按 A3 处理后通过 |
| `tests/hack/test_five_language_sources.py` | 38 | 我方内部文档在 `docs/` 下 | 按 D4 处理后通过 |
| `tests/hack/test_japanese_doc_sources.py` | 19 | 同上 | 按 D4 处理后通过 |

按 A3、D4 处理之后再跑一次全量，结果是 `25 failed, 9726 passed, 72 skipped`：23 个仍是 `tests/k8s`（没做 G4），另外 2 个是 `tests/policies/candidate_selectors/ascend_mindie/` 下的 `test_select_candidates_3x_64gx2[m0-expected0]` 和 `test_select_candidates_3x_64gx8[m10-expected10]`——它们要联网取模型配置，15 秒超时；第一次全量时是通过的。你遇到同样的超时，重跑并贴出在纯 `v2.3.0` 检出上的对照结果即可，不要改测试。

必须单独贴出结果的测试子集：

```bash
uv run pytest tests/worker/test_serve_manager.py -q          # 试合并实测 207 passed（上游 202 + 我方 5）
uv run pytest tests/policies/test_fork_selectors.py -q       # 27 passed
uv run pytest tests/migrations -q                            # 全过，含新增 test_single_head.py（3 passed）
uv run pytest tests/routes/test_least_pending_tie_break.py tests/routes/test_runtime_config_persistence.py tests/routes/test_video_queue_position.py -q   # 31 passed
uv run pytest tests/routes/test_admission_lookup.py tests/routes/test_minimax_h3_backfill.py tests/routes/test_video_input_refs.py tests/routes/test_video_progress.py -q
uv run pytest tests/server/test_builtin_backend_backfill.py -q   # 新增，见 C2；4 passed
uv run pytest tests/hack -q                                  # 616 passed
uv run pytest tests/http_proxy tests/scheduler tests/policies -q
```

### 8.2 D 组：数据库迁移（D1–D4 必过，D5 需环境）

用一次性的 PostgreSQL 16 容器。下面整段可直接执行；`OLD` 是升级前的我方代码检出（`origin/main`），`NEW` 是升级分支检出，两处都先 `uv sync`。

```bash
docker run -d --name pgup -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
sleep 5
docker exec pgup psql -U postgres -c "create database forkdb" -c "create database emptydb"

cat > /tmp/mig.py <<'PY'
import importlib.util, logging, os, sys
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

logging.basicConfig(level=logging.INFO)
url = sys.argv[1]
pkg = importlib.util.find_spec("gpustack").submodule_search_locations[0]
cfg = Config()
cfg.set_main_option("script_location", os.path.join(pkg, "migrations"))
cfg.set_main_option("sqlalchemy.url", url)
print("heads in code:", ScriptDirectory.from_config(cfg).get_heads())
with create_engine(url).connect() as c:
    print("db before:", MigrationContext.configure(c).get_current_heads())
command.upgrade(cfg, "head")
with create_engine(url).connect() as c:
    print("db after:", MigrationContext.configure(c).get_current_heads())
PY
```

| 编号 | 步骤 | 通过标准 | 试合并实测 |
|---|---|---|---|
| D1 | 在 `OLD` 目录：`uv run python /tmp/mig.py postgresql://postgres:pw@127.0.0.1:55432/forkdb`，然后往 `runtime_configs` 和 `video_generation_tasks` 各插 1 行 | `db after: ('b8d4f6a2c1e9',)` | 通过，38 张表 |
| D2 | 在 `NEW` 目录对 `forkdb` 跑同一条命令 | `heads in code: ['d9f0a1b2c3e4']`；`db before: ('b8d4f6a2c1e9',)`；`db after: ('d9f0a1b2c3e4',)`；日志里恰好 10 行 `Running upgrade`：上游 9 个（`c4d7e8f9a0b1 -> 367a3982fcde` … `f4a5b6c7d8e9 -> a7e2c9d4f681`）加 1 行 `b8d4f6a2c1e9, a7e2c9d4f681 -> d9f0a1b2c3e4`；D1 插的 2 行还在；`select enum_range(null::operationenum)` 仍含 `VIDEO_GENERATION` | 通过，51 张表，2 行都在 |
| D3 | 在 `NEW` 目录对 `emptydb` 跑同一条命令 | `db after: ('d9f0a1b2c3e4',)` | 通过，51 张表 |
| D4 | 两库结构对比 + 幂等：`diff <(docker exec pgup pg_dump -U postgres -s forkdb \| grep -v '^\\\\\|^--' \| sort) <(docker exec pgup pg_dump -U postgres -s emptydb \| grep -v '^\\\\\|^--' \| sort)`；再对 `forkdb` 跑一次 D2 | diff 为空；第二次没有任何 `Running upgrade` | 通过 |
| D5 `需环境` | **现网库副本**：把 m 集群 GPUStack 的库 `pg_dump` 出来恢复到一个临时 PostgreSQL，对它跑 D2 的命令；之后用升级后的镜像指向这个库启动 server | 同 D2；server 启动日志有 `Database migration completed.`，无 Traceback；模型、实例、视频任务、runtime config 条数与升级前一致（贴升级前后的 `select count(*)`） | 未执行（我没有现网库） |

D5 是上线的前置条件，由谁来做需要和用户确认；开发 agent 没有权限就在报告里标"未执行"，不要用 D2 代替。

### 8.3 I 组：镜像（I1–I3 必过，I4–I6 需环境）

| 编号 | 命令 / 步骤 | 通过标准 |
|---|---|---|
| I1 | `hack/check-overlay-list.sh` | `OK: overlay COPY list matches the 50 files changed vs v2.3.0.`（若你新增了 `gpustack/` 下的文件，数字相应增加，并说明多的是哪些） |
| I2 | `docker build -f pack/Dockerfile.acr --build-arg BASE_IMAGE=gpustack/gpustack:v2.3.0 -t gpustack-upgrade:overlay .`（本地验证用 Docker Hub 的上游镜像当基础镜像，不需要 ACR 权限；试合并实测通过） | 构建成功；日志里有 `base image alembic head: a7e2c9d4f681`、`migration chain OK, head=d9f0a1b2c3e4`、7 行 `… backend overlay OK` |
| I3 | 用 I2 的镜像指向一个**停在 `b8d4f6a2c1e9` 的库**（按 D1 的做法新建一个 `forkdb2`）启动：`docker run -d --name gsup --network host gpustack-upgrade:overlay --database-url postgresql://postgres:pw@127.0.0.1:55432/forkdb2 --port 18080 --api-port 18081 --gateway-mode disabled --bootstrap-password Trial-pw-123 --disable-update-check --disable-builtin-observability` | 日志有 `Running database migration.` 和 `Database migration completed.`，0 个 Traceback；库里 `alembic_version` = `d9f0a1b2c3e4`；`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18081/healthz` = 200；登录后 `GET /v2/inference-backends?perPage=100` 返回 11 个后端，含 `ACEStep Bernini BreezeTTS IndexTTS LightX2V YuE2 vLLMOmni`；`GET /v2/video-tasks` = 200。试合并实测全部符合（日志里唯一的 ERROR 是 `cleanup_orphan_workloads: No available deployer`，因为容器没挂 docker.sock，可忽略） |
| I4 `需环境` | ACR 里同步好 `reputationly/gpustack:v2.3.0` 基础镜像（见 D3 前置条件），触发 `pack-acr-overlay` 工作流，`ui_ref` 传 UI 升级分支 | amd64、arm64 两个架构都构建成功；贴镜像 tag 和 digest |
| I5 `需环境` | 拉取 I4 的镜像，`docker run --rm <镜像> python3 -c "import gpustack.ui, os; d=os.path.dirname(gpustack.ui.__file__); print(os.path.exists(d+'/index.html'))"` 并在浏览器里打开 | UI 是 v2.3.0 的新界面，且左侧菜单有"生成任务"（我方）和 KV cache / instance types（上游） |
| I6 `需环境` | `make build && make package`（完整镜像；fork 的发布路径是 overlay，这一步只为确认完整构建没坏） | 构建成功，或写明失败点是否为缺上游仓库密钥 |

### 8.4 功能回归（需环境：devm 集群，不要在 m 集群上做）

部署 I4 的镜像到 devm 后逐项执行。每项记录：操作、期望、实际、证据（请求响应 / 截图 / 日志片段）。

**S 组：7 个自研后端的调度**

| 编号 | 场景 | 期望 |
|---|---|---|
| S1 | LightX2V、Bernini、IndexTTS、Breeze TTS、YuE2、ACE-Step、vLLM-Omni 各部署 1 个实例 | 7 个都到 RUNNING；每个实例占用的卡数与升级前一致（LightX2V / Bernini / vLLM-Omni 按 `gpus_per_replica`，其余 1 张）；整卡独占（同一张卡上没有第二个实例） |
| S2 | 资源不足：把副本数调到超过空闲整卡数 | 多出来的副本停在 PENDING，提示信息是我方选择器的文案（含引擎名），不是 Traceback |
| S3 | 多卡：LightX2V 4 卡 profile（`gpus_per_replica: 4`） | 落在同一个 worker 的 4 张整卡上 |
| S4 | 手动指定 GPU（`gpu_ids`） | 落在指定卡上，且这些卡的显存被整卡记账 |
| S5 | 一张卡上已有别的模型占了部分显存 | 自研后端不选这张卡 |
| S6 | 已知限制确认：`select name, backend from models where gpu_type_selector is not null`（字段名以实际 schema 为准） | 结果里没有自研 7 个后端的模型。有的话立即上报，不要自行处理 |

**V 组：视频任务**

| 编号 | 场景 | 期望 |
|---|---|---|
| V1 | `POST /v1/videos` 提交 → `GET /v1/videos/{task_id}` 轮询 → `GET /v1/videos/{task_id}/content` | 任务走完 queued → running → succeeded，能下载到视频；进度字段随时间增长 |
| V2 | 连续提交超过实例数的任务 | 多出来的排队，排队位置正确；`/video-tasks` 列表和 UI"生成任务"页都能看到 |
| V3 | `POST /v1/videos/{task_id}/cancel`，分别对排队中和运行中的任务 | 状态变为取消，实例被释放给下一个任务 |
| V4 | `POST /v1/videos/inputs` 上传参考图后做图生视频 | 成功 |
| V5 | least-pending：2 个以上实例，连续提交 N 个任务 | 任务按"进行中数量最少"分配；数量相同时轮转，不会全压到一个实例 |
| V6 | 死任务重排：任务运行中杀掉对应实例容器 | sweeper 把任务重新排队并最终完成或按重试上限失败，状态和 `retry_count` 正确 |
| V7 | 存储清理：把一个已完成任务的时间改到保留期之外（或临时调小保留期） | janitor 清掉文件，任务记录状态正确 |
| V8 | 与上游新路由共存：`POST /v1/videos` 仍由我方 facade 处理（返回 `task_id`）；上游的 `/v1/videos/sync` 路由存在且不影响前者 | 两者互不影响 |
| V9 | 带 `X-GPUStack-Model-Instance` 头指定实例 | 请求落到指定实例 |
| V10 | 用量统计 | 视频任务产生 `VIDEO_GENERATION` 类型的用量记录，在上游新的用量页面里可见 |

**C 组：配置与内置后端**

| 编号 | 场景 | 期望 |
|---|---|---|
| C1 | UI 里改一项运行时配置 → 重启 server | 重启后值还在（`runtime_configs` 表有记录，启动日志有回放） |
| C2 | 升级前把某个自研后端的 `common_parameters` 置空，升级后启动 | 被回填成内置默认值 |
| C3 | 升级前手工改过 `common_parameters` 的后端 | 升级后保持手工值，不被覆盖 |
| C4 | 上游内置后端（vLLM、SGLang 等）的版本列表 | 是 v2.3.0 的内容（上游的后端源同步机制在工作），自研 7 个后端也都在 |
| C5 | 启动日志 | 有视频 sweeper、存储 janitor、scaling scheduler、operator settings 四个任务的启动记录；`_probe_lightx2v_nfs` 的探测日志在 |

**M 组：多机与 worker 生命周期（`serve_manager.py` 的核心验证）**

用一个跨 2 台以上 worker 的 vLLM 实例。

| 编号 | 场景 | 期望 |
|---|---|---|
| M1 | 首次多机启动 | 主节点和全部从节点起齐，实例 RUNNING；`ports` 列表长度满足引擎要求；每个从节点记录的 `main_launch_pid` 等于实例的 `pid` |
| M2 | 在 UI 里重启该实例（主进程组重建） | 所有从节点的旧容器被停掉并重新创建，重新起齐；从节点日志有 `main process group changed` |
| M3 | 重启主节点所在 worker 的 GPUStack 进程 | 实例恢复 RUNNING，无残留的旧从节点容器，端口没有重复占用 |
| M4 | 启动期间杀掉一个从节点的容器 | 按部署的恢复策略收敛：要么该从节点被重拉后起齐，要么实例进入 ERROR 并给出原因；不能无限卡在 starting |
| M5 | 把实例停掉再启动，观察端口 | 端口沿用，不重复占用；删除实例后端口释放，新实例可以用同一端口 |
| M6 | 同时创建 2 个多机实例（端口占位并发） | 两个实例拿到不相交的端口，都能起齐 |
| M7 | 在同一个从节点上短时间内连续触发两次启动事件（例如快速连续改一次配置） | 只创建一个容器（上游的启动防重入在工作） |

M 组任何一项不通过，先按 C4 末尾的"回退补丁"对号处理，并在报告里写明用了哪一条。

**N 组：上游 v2.3.0 新功能冒烟（确认我方改动没把上游功能弄坏）**

| 编号 | 场景 | 期望 |
|---|---|---|
| N1 | 部署一个普通 vLLM 文本模型并对话 | 正常；`--kv-transfer-config` 等现有后端参数照传不报错 |
| N2 | 修改该部署的参数 → 查看 revision 历史 → 回滚 | 历史有记录，回滚生效 |
| N3 | 导出部署 YAML 再导入 | 成功 |
| N4 | 打开 UI 的 KV cache、GPU instance types、用量、benchmark 页面 | 页面能打开，无 404 / 500 |
| N5 | 定时扩缩容：给一个模型配一条定时规则 | 到点副本数变化 |

### 8.5 自测报告格式

报告是一个 Markdown 文件，随分支一起提交到 `docs-fork/upgrade-v2.3.0-selftest-report.md`，包含：

1. 后端分支名、最终 commit SHA；UI 分支名、最终 commit SHA。
2. `git diff --stat origin/main...HEAD | tail -1` 和 `git diff --stat v2.3.0 HEAD | tail -1`。
3. 上面每个编号一行：`编号 | 通过 / 不通过 / 未执行 | 证据位置`，未执行的写原因。
4. `make lint`、`make test` 的完整输出（附件）。
5. 与本文档不一致的地方：你在哪里没有照文档做、为什么。
6. 用到的回退补丁（如有）。
7. 镜像 tag 与 digest（amd64、arm64）。

## 9. 上线与回滚

### 9.1 上线前置条件

1. §8 中标"必过"的项全部通过，D5（现网库副本演练）通过。
2. 升级前对现网 GPUStack 数据库做一次完整备份（`pg_dump -Fc`），记录备份文件位置和大小，并验证能恢复到一个临时实例。
3. 记录当前现网镜像的 tag 和 digest（后端 overlay 镜像、worker 镜像），作为回滚目标。
4. 先上 devm，按 §8.4 跑完再上 m。m 上有 28 台大模型节点在用 Mooncake KV 缓存池，升级窗口内不要动 worker 上的 `mooncake_client` 容器和 vLLM 的 `--kv-transfer-config` 参数。

### 9.2 上线顺序

server 和 worker 用同一个镜像 tag。上游 v2.3.0 改了 server 与 worker 之间的端口协调协议（跨 worker 端口占位），**不要长期混跑新旧版本**：

1. 停 server（旧版本）。
2. 起新版本 server，等日志出现 `Database migration completed.` 和 `Serving GPUStack API`。
3. 逐台升级 worker。已在运行的模型实例容器不会因为 worker 进程重启而重建，但升级过程中不要做部署变更。
4. 全部 worker 升完后做 §8.4 的 S1、V1、M1、N1 四项冒烟。

这个顺序是按上游升级说明和代码推断的，没有在真实多节点环境验证过；在 devm 上走一遍并把实际观察写进自测报告。

### 9.3 回滚

**数据库不能靠 downgrade 回滚。** merge revision 的 `downgrade()` 是空操作，上游 9 个迁移的 downgrade 没有验证过，而且升级后产生的新数据（revision 历史、用量表等）在旧结构里没有位置。回滚数据库的唯一手段是恢复 9.1 第 2 步的备份。

| 情况 | 做法 |
|---|---|
| 新 server 起不来（迁移失败或启动报错） | 停新 server → 用备份恢复数据库 → 用 9.1 第 3 步记录的旧镜像起 server。worker 还没动，不需要处理 |
| server 正常、worker 升级后多机实例起不来 | 先按 C4 的回退补丁修；修不了则整体回滚：停全部新 worker 和 server → 恢复数据库备份 → 旧镜像起 server → 旧镜像起 worker。备份之后创建的模型部署和视频任务会丢，事先告知使用方 |
| 只有 UI 有问题 | 重新触发 overlay 构建，`ui_ref` 指回旧 UI 分支。注意旧 UI 不认识 v2.3.0 的新接口，只能作为临时措施 |

旧镜像（基于 v2.2.3）**不能**直接连已升级的数据库：它的代码里只有 `b8d4f6a2c1e9` 这一个 head，库里的 `d9f0a1b2c3e4` 它不认识，启动时 Alembic 会报找不到 revision。

### 9.4 以后每次跟上游正式版的固定动作

1. `git fetch upstream --tags`，目标只选正式 tag。
2. `git merge <tag>`，按 §5 的归类处理冲突（多数文件届时已与上游一致，不会再冲突）。
3. 如果上游新增了迁移：新建一个双父 merge revision，父节点是"我方当前 head"和"上游新 head"；同步修改 `pack/Dockerfile.acr` 里的两处断言和 COPY 清单、`tests/migrations/test_single_head.py` 里的期望值。
4. `hack/upstream-base.env`、`pack/Dockerfile.acr` 的 `BASE_IMAGE`、`pack-acr-overlay.yml` 的默认值三处一起改；把上游镜像同步进 ACR。
5. `hack/check-overlay-list.sh`，按输出增删 COPY。
6. UI fork 同步到同一个 tag。
7. 跑 §8 全套。

## 10. 验收清单

验收由本文档作者执行。Git 能合并、CI 是绿的都不算完成；下面每一条都会实际去查，任何一条 `必须` 项不满足即打回。

### 10.1 交付物

| # | 必须提交 |
|---|---|
| 1 | 后端分支 `chore/upstream-v2.3.0` 的 PR 链接和最终 commit SHA |
| 2 | 前端分支 `chore/upstream-v2.3.0` 的 PR 链接和最终 commit SHA |
| 3 | 自测报告 `docs-fork/upgrade-v2.3.0-selftest-report.md`（格式见 §8.5） |
| 4 | `make lint`、`make test` 完整输出 |
| 5 | D1–D4 的迁移日志；D5 的结果或"未执行 + 原因" |
| 6 | overlay 镜像 tag 与 digest（amd64、arm64） |
| 7 | devm 上 S / V / C / M / N 各组的证据 |
| 8 | 未验证项清单及原因 |

### 10.2 我会执行的检查（在你的分支上）

**历史与范围**
```bash
git merge-base --is-ancestor v2.3.0 HEAD && echo ok            # 上游 v2.3.0 是祖先（是 merge，不是 rebase / squash）
git merge-base --is-ancestor origin/main HEAD && echo ok        # 我方历史完整
git log --oneline --first-parent origin/main..HEAD             # 提交拆分符合 §3.2
git diff v2.3.0 HEAD --stat -- gpustack | tail -1               # 与上游的差异量：应与升级前 (git diff v2.2.3 origin/main -- gpustack) 同量级
```

**A 组**
```bash
git diff v2.3.0 HEAD -- gpustack/gateway/__init__.py pyproject.toml uv.lock mkdocs.yml | wc -l    # 0
cat CLAUDE.md                                                                                       # 只有 @AGENTS.md
grep -c "Fork notes" AGENTS.md                                                                      # ≥1
git diff v2.3.0 HEAD -- .github/workflows/pack.yaml | grep -c "^[+-][^+-]"                          # ≤10，且 on: 块与上游一致
```

**调度**
```bash
grep -c "resource_view=resource_view" gpustack/scheduler/scheduler.py             # 17（上游 10 + 自研 7）
grep -rn "get_worker_allocatable_resource(" gpustack/policies/candidate_selectors/ | grep -v "self\.\|def "   # 无输出
git diff v2.3.0 HEAD -- gpustack/scheduler/scheduler.py | grep -c "^-[^-]"         # 0：上游的行一行都没删，只有新增
uv run pytest tests/policies tests/scheduler -q
```

**controllers / server**
```bash
grep -c "_init_community_backends" gpustack/server/controllers.py                  # 0
grep -c "_seed_builtin_source\|gather_and_merge" gpustack/server/controllers.py    # 11，与 v2.3.0 相同
grep -c "Backfilled common_parameters" gpustack/server/controllers.py              # 1
grep -n "_start_video_task_sweeper\|_start_video_storage_janitor\|_start_scaling_scheduler\|_start_gpustack_operator_settings\|_apply_persisted_config\|_probe_lightx2v_nfs" gpustack/server/server.py
uv run pytest tests/server -q
```
四个 `_start_*` 必须都在 `_start_leader_tasks()` 里被调用；`_apply_persisted_config()` 在 `_prepare_data()` 之后。

**serve_manager**
```bash
grep -c "_start_guard\|_ports_already_reserved\|_required_port_count\|_start_model_instance_body" gpustack/worker/serve_manager.py   # 0
grep -c "DistributedPortClient\|ModelPortClaim" gpustack/worker/serve_manager.py                                                      # 7，与 v2.3.0 相同
grep -n "_follower_launch_is_stale\|main_launch_pid" gpustack/worker/serve_manager.py
git diff v2.3.0 HEAD -- gpustack/worker/serve_manager.py | grep -c "^[+-][^+-]"       # 试合并参考值 85；明显更大要逐行解释
git diff v2.3.0 HEAD -- gpustack/worker/serve_manager.py | grep "^-[^-]"              # 被删 / 被改的上游行，每一行都要有理由
uv run pytest tests/worker -q
```
我会对照 `ref-patches/serve_manager.vs-v2.3.0.diff` 逐块看，重点是：`_assign_ports` / `_assign_local_ports` / `_assign_distributed_ports` / `_select_ports` 与上游逐字一致；陈旧判断用的是"从节点记录的 `main_launch_pid` 与当前 `mi.pid` 比较"；同步循环里的豁免只对"尚未针对当前主进程组启动过的从节点"生效，不是对所有启动期状态一律跳过。

**迁移**
```bash
uv run pytest tests/migrations -q
git diff origin/main HEAD --name-only -- gpustack/migrations/versions/ | grep -v "367a3982fcde\|a3f5c1d9e0b2\|d5e8f0a1b2c3\|b7e2c4d15a80\|e4a1c8b7d0f3\|f1a2b3c4d5e6\|b4c5d6e7f8a9\|f4a5b6c7d8e9\|a7e2c9d4f681\|d9f0a1b2c3e4"
#   ↑ 期望无输出：除上游 9 个 + merge revision 外没有别的迁移文件变化，我方 4 个旧迁移零改动
grep -c "sed -i" pack/Dockerfile.acr                                                # 0
```
然后我会自己重跑 §8.2 的 D1–D4（PostgreSQL 16），并核对你贴的日志。

**overlay**
```bash
hack/check-overlay-list.sh
grep -n "a7e2c9d4f681\|d9f0a1b2c3e4" pack/Dockerfile.acr
cat hack/upstream-base.env
docker build -f pack/Dockerfile.acr --build-arg BASE_IMAGE=gpustack/gpustack:v2.3.0 -t accept:overlay .
```
并用构建出的镜像重做 I3（指向停在 `b8d4f6a2c1e9` 的库启动，查 `alembic_version`、`/healthz`、`/v2/inference-backends`、`/v2/video-tasks`）。

**文档位置**
```bash
git diff --name-only --diff-filter=A v2.3.0 HEAD -- docs | wc -l     # 0
uv run pytest tests/hack -q
```

**前端**
```bash
git merge-base --is-ancestor v2.3.0 HEAD && echo ok
git diff v2.3.0 HEAD -- package.json pnpm-lock.yaml | wc -l           # 0
grep -rn "from 'vitest'" src | wc -l                                   # 0
pnpm install --frozen-lockfile && pnpm run check:locales && pnpm test && pnpm run build
```
并浏览器打开 I4 的镜像：视频 playground、"生成任务"页、KV cache、instance types、用量页各看一遍，中 / 英 / 日 / 俄四种语言下左侧菜单没有裸 key。

### 10.3 打回条件

- 有 `必过` 项未通过，或把未执行的项写成通过。
- `serve_manager.py`、`scheduler.py`、`controllers.py`、`server.py` 里删改了上游行而没有说明。
- 我方已有迁移文件被改动，或 `pack/Dockerfile.acr` 里仍有构建期改写 `down_revision` 的逻辑。
- 测试失败被归为"上游基线 / 环境问题"但没有在纯 `v2.3.0` 检出上的对照输出。
- 为了让测试通过而删除、跳过或放宽了上游或我方的既有测试。
- M 组（多机生命周期）没有真机证据。这一组没有可替代的验证方式。

### 10.4 上线判定

验收通过只代表分支可以合并。上线 m 集群另需：D5（现网库副本演练）通过、devm 上稳定运行一段时间（时长由用户定）、§9.1 的备份已做并验证可恢复。

## 附录 A：`ref-patches/` 索引

`*.vs-v2.3.0.diff` 是"解完之后的文件"相对上游 `v2.3.0` 的差异，也就是合并后 fork 在该文件上应该剩下的全部改动；`*.vs-origin-main.diff` 是相对我方 `origin/main` 的差异。

| 文件 | 对应章节 | 内容 |
|---|---|---|
| `dockerignore.vs-v2.3.0.diff` | B1 | |
| `gpustack_routes_benchmarks.py.vs-v2.3.0.diff` | B2 | |
| `gpustack_http_proxy_strategies.py.vs-v2.3.0.diff` | B3 | 上游按 ID 轮转 + 我方 `select_least_pending_instance` |
| `tests_worker_test_serve_manager.py.vs-v2.3.0.diff` | B4 | 我方 5 个生命周期用例 |
| `tests_worker_backends_test_backend.py.vs-v2.3.0.diff` | B5 | |
| `pack.yaml.vs-v2.3.0.diff` | A3 | 7 行 |
| `gpustack_scheduler_scheduler.py.vs-v2.3.0.diff` | C1 | 只有新增行 |
| `fork-files.vs-origin-main.diff` | D1、D2 | `lightx2v_resource_fit_selector.py` 的适配；3 个测试 fixture |
| `test_fork_selectors.py.ref` | D1 | 新增测试，放到 `tests/policies/`，27 个用例 |
| `gpustack_server_controllers.py.vs-v2.3.0.diff` | C2 | |
| `test_builtin_backend_backfill.py.ref` | C2 | 新增测试，放到 `tests/server/`，4 个用例 |
| `gpustack_server_server.py.vs-v2.3.0.diff` | C3 | |
| `serve_manager.vs-v2.3.0.diff` | C4 | 85 行增删 |
| `2026_11_10_1200-d9f0a1b2c3e4_merge_fork_and_upstream_v2_3_0.py` | D2 | merge revision，放到 `gpustack/migrations/versions/` |
| `test_single_head.py.ref` | D2 | 新增测试，放到 `tests/migrations/`，3 个用例 |
| `overlay-files.vs-origin-main.diff` | D3 | `pack/Dockerfile.acr`、`hack/upstream-base.env`、`pack-acr-overlay.yml` |
| `ui-conflicts.vs-ui-v2.3.0.diff` | §7.1 | 前端 5 个冲突文件解完后的差异 |
| `ui-task-inputs-test.vs-fork-main.diff` | §7.2 | 视频测试从 Vitest 改到 `node:test` |

三个 `.py.ref` 是完整的测试文件，复制到目标目录时去掉 `.ref` 后缀。加这个后缀是因为仓库的 `pytest.ini` 没有限定 `testpaths`，`make test` 会收集整个仓库里的 `test_*.py`。

D4（文档搬家）没有补丁，按该节的命令执行即可。

## 附录 B：试合并上验证过什么、没验证什么

试合并 = 在一个草稿 worktree 上，从 `origin/main`（`8b02caf3`）合并 `v2.3.0` 并按本文档逐节处理。它没有提交、没有推送，不是交付物。

**验证过（本机实跑）**

| 项 | 结果 |
|---|---|
| `uv sync`、`uv lock --check`、`compileall`、核心模块 import | 通过 |
| 全量 `pytest tests` | 25 failed / 9726 passed / 72 skipped；25 个里 23 个缺 chart 包（没跑 `make install`），2 个联网超时。没有一个失败落在我方或合并改动的文件上 |
| `tests/worker/test_serve_manager.py` | 207 passed（上游全部用例 + 我方 5 个） |
| `tests/policies/test_fork_selectors.py` | 27 passed |
| `tests/server/test_builtin_backend_backfill.py`、`tests/migrations/test_single_head.py` | 4 passed、3 passed |
| `tests/hack` | 616 passed |
| 迁移，PostgreSQL 16 | 停在 `b8d4f6a2c1e9` 的库升级：恰好 10 步（上游 9 个 + merge），到 `d9f0a1b2c3e4`，预置的 `runtime_configs` / `video_generation_tasks` 数据还在；空库全量升级到同一 head；两库 `pg_dump -s` 结构一致；重复执行无动作 |
| 旧代码连已升级的库 | 报 `Can't locate revision identified by 'd9f0a1b2c3e4'`（所以不能只回滚镜像不回滚库） |
| `hack/check-overlay-list.sh` | OK，50 个文件 |
| overlay 镜像构建（基础镜像用 Docker Hub 的 `gpustack/gpustack:v2.3.0`，amd64） | 成功，两处迁移断言和 7 个后端的 import 检查都过 |
| overlay 镜像启动（指向停在 fork head 的库） | 迁移完成、`/healthz` 200、能登录、11 个后端（含 7 个自研）、`/v2/video-tasks` 200、0 个 Traceback |
| 前端（Node 22.23.4） | `pnpm install --frozen-lockfile`、`check:locales`、`pnpm test`（0 失败）、`pnpm run build` 全过 |

**没验证（需要你或用户的环境）**

| 项 | 为什么重要 |
|---|---|
| 真实多机 vLLM 的启动 / 重启 / 从节点恢复（M 组） | `serve_manager.py` 是以上游为主体重新挂我方逻辑的，单元测试覆盖不了真实的跨 worker 时序 |
| 现网数据库副本升级（D5） | 本机用的是 fork 代码新建的库加 2 行手插数据，没有现网的数据量和历史数据形态；上游 9 个迁移里有数据回填 |
| 7 个自研后端真正拉起并出结果（S、V 组） | 本机没有 GPU；选择器只在单元测试层面验证了分发和选卡 |
| 开着网关时 `/v1/videos` 与 `/v1/videos/sync` 共存（V8） | 本机启动时关了网关 |
| ACR 上 amd64 + arm64 的 overlay 构建，以及带 UI dist 的镜像（I4、I5） | 本机只构建了 amd64，且没有放 UI dist |
| `make lint`、`make generate`、`make install`、`make build`、`make package` | 没整体执行 |
| UI 浏览器手测 | 只做了构建和单元测试 |
| server 与 worker 新旧版本混跑、滚动升级顺序（§9.2） | 只读了代码 |
