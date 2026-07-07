# LightX2V 输入图统一落 NFS —— 设计方案

状态:**设计已定稿,待实现**。日期:2026-07-07。
涉及仓:`gpustack`(门面 + janitor)、`new-api`(gpustackplus adaptor)。引擎 `LightX2V` **零改**。

---

## 0. Scope 声明(先划边界)

**本机制仅作用于 `gpustackplus` 渠道路由到的模型。** 第三方渠道(sora / minimax / 各家云 API)保持
base64/URL **透传**,adaptor 层物理隔离,不按模型名判断。

原因是硬性的:**NFS 是 new-api 与 GPUStack 计算集群之间的共享盘(同一块 SFS)。第三方上游不在这块盘上**,
给它 `/nfs-output/inputs/…` 路径它读不到。NFS 引用只对"我们自建、与 new-api 共享同一 SFS 的 LightX2V"成立。

new-api 本就按渠道类型选 adaptor(`Distribute` 中间件),NFS 物化逻辑只写在
`relay/channel/gpustackplus/`(同步图片链路)和 `relay/channel/task/gpustackplus/`(异步视频链路)
两个 adaptor 里,其它渠道一行不动。"只对 gpustackplus 生效"由**代码所在位置**天然保证。

> 这两个 adaptor **属于同一渠道类型 `ChannelTypeGPUStackPlus`,是它的两条链路**(图片同步 relay、
> 视频异步 task),不是两个渠道。因此 NFS 写盘 + 生成 input_ref 的逻辑应**抽成一个共享工具函数**
> (base64 / 文件字节 / URL 下载三合一 + §3 路径约定),两个 adaptor 都调它,避免两处分叉;
> NFSRoot、media-config 等也是同一份配置共用。

> **另见** `new-api` 仓 `docs/gpustackplus-sync-image-backpressure.md`:同步图片链路在反压下的加固
> (快速失败 / 断开感知 / 并发上限 / 门面 cancel)。与本方案**同批改动、一次出包**(都动图片 adaptor)。

> 运维约定:`gpustackplus` 渠道只用于自建 LightX2V,**不要在此渠道挂第三方 upstream**(否则 NFS 路径会发给读不到盘的上游)。真要接第三方,新建对应类型的渠道。

---

## 1. 目标与原则

- 一份输入**只落一次盘,且落在 NFS**;引擎直读,不下载、不在计算节点本地盘留副本;
- 清理**复用现有 gpustack janitor**(TTL + 水位 + 非终态保护),不新造清理器;
- 安全不回退:外部调用方永远拿不到"传路径"的能力(IDOR 关死);只有可信的 new-api 能写 NFS 并引用;
- 数据库不再存 base64:任务 `params` 只存 NFS 路径(小字符串),消除撑库 + 重派重发大 blob;
- 多图 / 单图 / 首尾帧 / mask / audio 统一一套机制,引擎零改。

### 背景:为什么不用 OBS-URL

引擎读 URL 时会 `download_media` 落到 `cache_dir/inputs/imgs/`(计算节点本地盘),而
`file_service.cleanup()` 只在关停时关 httpx 客户端、**不删任何输入文件**,全仓无 TTL / 周期清理。
即 URL 输入会在**计算节点本地盘无限累积、迟早撑爆,且无监控无 janitor**。加上引擎 runner 只支持
逗号分隔的**本地路径**多图、不支持多 URL,OBS-URL 反而要改引擎 + 自造清理。NFS 全避开这些。

---

## 2. 总体数据流(新)

```
终端用户 ──(base64 / multipart 文件 / URL)──▶ new-api (gpustackplus adaptor)
                                              │ ① 统一物化:全部落 NFS inputs/…
                                              │    base64 解码写 / 文件字节写 / URL 下载后写
                                              ▼
              new-api ──(POST /v1/videos,input_refs = 相对路径列表)──▶ gpustack 门面
                                              │ ② 校验引用(root 前缀/realpath 在 inputs 下/
                                              │    user 段绑定/存在/数量),映射为绝对 NFS 路径,
                                              │    逗号 join 写 image_path 等
                                              ▼
              门面 ──(image_path=逗号本地 NFS 路径)──▶ 引擎 runner split(",") 逐个 Image.open 直读
                                              ▼
              引擎写结果到 NFS ──▶ new-api 读 nfs_path ──▶ 搬 OBS ──▶ 签名 URL 给用户
                                              │ ④ janitor 保护/清理 inputs 与 outputs(复用现有)
```

门面**不再解码 base64、不再下载 URL**——输入到达门面时已是 NFS 上的文件,门面只做"校验 + 转 image_path"。

---

## 3. 路径约定(三方一致)

沿用现有约定,只把"谁写、用什么 id"讲死:

```
<root>/inputs/<task_type>-<model>/YYYY/MM/DD/<user_id>/<input_group>-<field>[-<i>].<ext>
```

- `<root>`:new-api `MediaStorageSettings.NFSRoot()`(`NFSOutputRoot`,默认 `/nfs-output`)
  **必须等于** gpustack `lightx2v_output_root`(默认 `/nfs-output`)——同一 SFS、同一挂载绝对路径。
  **硬不变量**,见 §6 启动探测。
- `<task_type>`:t2i / i2i / t2v / i2v / flf2v / s2v(与门面 `_VALID_TASK_TYPES` 对齐)。
- `<user_id>`:new-api 终端用户 id(**租户隔离段**,门面校验时用)。
- `<input_group>`:**new-api 生成的唯一 id**(用它的 `PublicTaskID` 或新 uuid)。
  解决"门面 task_id 提交前不存在"的先有鸡先有蛋:**输入路径用 new-api 的 id,输出路径
  (save_result_path)仍用门面 task_id**,两者解耦。
- `<field>`:image / last_frame / image_mask / audio(与门面 `_INPUT_FIELDS` 键对齐)。
- `<i>`:仅多图 image 有(0..N-1);单图无后缀。
- `<ext>`:.png / .wav 等。

保留 `inputs/` 前缀 + 日期分层的原因:janitor 现有 `_DAY_GLOBS` 覆盖 `inputs/*/YYYY/MM/DD`,
`_protected_day_dirs` 按 `params` 里 `*_path` 值算 `parent.parent` 保护 —— 沿用约定 = **janitor 零改**
(逗号拆分上一轮已做)。

---

## 4. 门面契约:可信输入引用 + 校验(安全核心)

new-api 传给门面的**不是 base64、也不是绝对路径,而是"相对 inputs 根的路径列表"**,门面自己拼绝对
路径,天然防穿越。请求体新增字段(示意):

```json
"input_refs": {
  "image":      ["inputs/i2i-qwen-image-edit/2026/07/07/1/<gid>-image-0.png",
                 "inputs/i2i-qwen-image-edit/2026/07/07/1/<gid>-image-1.png"],
  "last_frame": ["inputs/flf2v-wan/2026/07/07/1/<gid>-last_frame.png"]
}
```

门面对每个引用**逐条校验**(任一不过 → 400):

1. **必须相对路径**;调用方给绝对路径直接拒(只收相对,门面 `os.path.join(_output_root(), ref)`)。
2. **realpath 归一后仍在 `<root>/inputs/` 之下**(挡 `..` 穿越 / 软链逃逸)。
3. **只允许 `inputs/` 子树** —— 从物理上禁止把别人的**输出**(在 `<root>/<feature>/…`,不在 inputs 下)
   当输入读,堵掉"拿他人结果做 i2i 反推"。
4. **路径里的 `/<user_id>/` 段 == 请求体 user_id** —— 跨租户读 inputs 也堵死(纵深防御)。
5. **文件存在**(不存在 → 400,暴露 new-api bug / 竞态,不把空任务派给 GPU)。
6. **数量 ≤ `_MAX_INPUT_IMAGES`(=5)**(image 维度);带 mask 时 image 只允许 1 张(引擎约束)。

校验通过:门面把绝对路径逗号 join 写进 `image_path` / `last_frame_path` / `image_mask_path` /
`audio_path`,并**继续把这些字段视为 engine-owned**(`_ENGINE_OWNED_FIELDS` 不变,外部仍不能直接传
`image_path`)。`input_refs` 是**独立的、只走校验通道**的字段。

**为什么这样安全**:调用方给"相对路径",门面用**自己的 root** 前缀 + realpath 收敛,调用方无法指向
root 之外;`inputs/` 子树限制 + user_id 绑定挡住 inputs 内跨租户 / 跨类型读。真正的信任边界是:
**门面 API key 只有 new-api 持有**,end user 打的是 new-api 不是门面——上述校验是"钥匙泄露 / 程序 bug"
时的纵深防御。

### 4.1 删除旧 base64/URL 输入路径(决策 1)

门面**删除** `_materialize_input` 中的 base64 解码与 URL 下载分支,以及 `_persist_input` 里对
`image`/`last_frame`/`image_mask`/`audio` 原始 base64/URL 的处理。门面**只接受 `input_refs`**。
`_INPUT_FIELDS` 语义从"原始输入字段"改为"仅用于 input_refs 的 field→(engine_field, ext) 映射"。
过渡兼容不保留(内部系统,new-api 与门面同批发布)。

---

## 5. new-api 侧:统一物化(所有输入形态 → NFS)

gpustackplus 的两个 adaptor(图片 `channel/gpustackplus`、视频 `channel/task/gpustackplus`)成为
**唯一物化点**,三种输入形态收敛成"写 NFS + 生成 input_ref":

| 到达形态 | new-api 处理 |
|---|---|
| **base64 / data-uri**(体验区、接口 JSON) | 解码字节 → 写 NFS |
| **multipart 文件**(接口 form-data) | 读文件字节 → 写 NFS |
| **URL**(接口 JSON 传 http(s)) | **new-api 下载** → 写 NFS(引擎永不下载;new-api 够不到 URL → 400) |

- 写盘根:`MediaStorageSettings.NFSRoot()`;相对路径按 §3 约定拼(`inputs/<task_type>-<model>/日期/<user_id>/<gid>-<field>[-i].<ext>`)。
- 顺序:**先写 NFS 输入 → 再 POST 提交**;input_group `<gid>` 用 `info.PublicTaskID` 或新 uuid。
- 提交体带 `input_refs`(相对路径列表);不再发 base64/URL 给门面。
- 体验区与接口在 new-api 内**收敛到同一物化函数**,二者对门面无差异(区别只在"字节从哪来")。

---

## 6. NFSRoot 不变量:启动探测(决策 4)

**门面(gpustack)和 new-api 两侧,启动时各自探测 `<root>/inputs/` 可读写,不满足则拒绝启动 + log。**

- 探测动作:确保 `<root>` 已挂载且 `<root>/inputs/` 可创建 + 可写 + 可读(写一个临时探针文件再删)。
- gpustack:在 server 启动(video 相关组件初始化处,与 janitor 同生命周期)探测 `lightx2v_output_root`。
- new-api:在媒体存储设置生效 / 服务启动处探测 `NFSRoot()`;`IngestNFSPath`/NFS 相关开启时强制。
- 失败:**明确 fatal + 日志写清**"NFS 未挂载 / 不可写 / root 配置与对端不一致",不进入半可用状态。
- 成功:各自 log 一行当前 root 绝对路径,便于人工核对两侧一致。

> 这是把"两侧 root 必须同一 SFS 同一绝对路径"从口头约定升级为启动即校验的硬门槛。

---

## 7. janitor 复用(几乎零改)

- **保护**:`_protected_day_dirs` 已按 `params` 里 `_INPUT_PATH_PARAM_KEYS`
  (`image_path`/`last_frame_path`/`image_mask_path`/`audio_path`/…)的值算 day-dir 保护,且已加
  **逗号拆分**(多图路径,`value.split(",")`)。非终态任务的输入 day-dir 一直被保护,不会误删。
- **清理**:输入图属 `inputs/…/YYYY/MM/DD` day-dir,任务终态后不再保护 → 按 **TTL** 随 day-dir 删除;
  水位吃紧也可驱逐。输入不需要 DONE 宽限(不像结果要留给 new-api 取件)。
- **改动**:janitor **无需再改**。仅确认 `_INPUT_PATH_PARAM_KEYS` 已含全部输入字段(现状已含)。

### 7.1 孤儿输入清理(决策 2)

new-api "写了输入但提交失败"(崩在写与 POST 之间)→ 输入文件成孤儿、无任务行引用 → 不被保护 →
**随 day-dir TTL 到期由 janitor 清理**。new-api **不主动删**(保持简单)。

---

## 8. 场景全覆盖

**输入种类**

- 单图 i2i:image_ref 1 个 → image_path 单路径。
- 多图 i2i(≤5):image_ref N 个 → image_path 逗号串,runner 逐个读。
- i2v:image_ref 1 个(首帧)。
- flf2v:image_ref(首帧)+ last_frame_ref(尾帧);flf2v 必须 2 帧,缺一 400。
- s2v:audio_ref(field=audio),同机制。
- 带 mask 的 edit:image_ref + image_mask_ref;有 mask 时 image 只允许 1 张(引擎约束,new-api + 门面双防呆)。
- t2i / t2v:无 input_refs,整条机制不触发。

**失败 / 并发 / 重派**

- 写输入后提交失败:孤儿文件 → janitor TTL 清理(§7.1)。
- 死亡重派:`params.image_path` 是 NFS 路径,文件还在(非终态被保护),重派**原样复用,不重写不重传**
  —— 优于 base64(base64 重派会重发大 blob)。
- 并发多任务:`<input_group>` 唯一 → 文件名不撞。
- NFS 写失败 / 没挂 / 写满:new-api 物化阶段就 400/5xx 明确报错,**不提交**拿不到输入的任务。
- URL 下不到:物化阶段 400,不进 GPU。
- janitor 与在飞任务竞态:非终态保护已覆盖;多图逗号已拆分。

**安全 / 隔离**

- 外部传 `image_path` 等原生字段:仍被 `_ENGINE_OWNED_FIELDS` 拒。
- 外部伪造 input_ref 指向他人文件:相对路径 + realpath-under-inputs + user_id 段绑定,三重挡住。
- 指向输出文件当输入:`inputs/` 子树限制物理隔离。

**配置 / 兼容**

- 硬不变量:new-api `NFSRoot()` == gpustack `lightx2v_output_root`(§3 / §6 启动探测)。
- DB 瘦身:`params` 不再含 base64,重派不重发大 blob(顺带修掉 params 撑库隐患)。
- 无过渡 fallback:门面删旧 base64/URL 输入路径,new-api 与门面**同批发布**。

---

## 9. 与 M4 安全模型的关系(纠偏说明)

M4"门面独占路径、拒绝外部传路径"是为防 IDOR。本方案**不违背**:end user 依旧不能传路径;是**可信的
new-api**(唯一持门面 key 者)把输入落 NFS 并传"受校验的相对引用"。门面对该引用做 root 前缀 + realpath +
inputs 子树 + user 绑定校验——**信任边界没有外移,只是给可信编排层开了一个受控入口**。

---

## 10. 改动清单(粗粒度)

| 仓 | 文件 | 改动 | 出包 |
|---|---|---|---|
| **gpustack**(门面) | `routes/videos.py` | 新增 `input_refs` 解析 + 校验(root 前缀 / realpath / inputs 子树 / user 段 / 存在 / 数量 / mask 单图);映射到 `image_path` 等;**删除** base64/URL 输入分支 | overlay 出包 |
| **gpustack**(启动探测) | server 启动处 | `inputs/` 可读写探测,失败 fatal + log(§6) | overlay 出包 |
| **gpustack** | `server/video_storage_janitor.py` | **不改**(逗号拆分已做) | — |
| **new-api** | `relay/channel/gpustackplus/adaptor.go`、`relay/channel/task/gpustackplus/adaptor.go` | base64/URL → **物化到 NFS + 发 input_ref**;新增 NFS 写工具(base64 / 文件 / URL 下载三合一);mask 单图防呆 | 重新构建部署 |
| **new-api**(启动探测) | 媒体存储设置 / 启动处 | `NFSRoot()/inputs/` 可读写探测,失败 fatal + log(§6) | 重新构建部署 |
| **LightX2V** 引擎 | — | **零改** | 不动 |
| 文档 | 本文件 + 两仓 README/部署文档 | NFSRoot==output_root 不变量、input_refs 契约、路径约定 | — |

---

## 11. 决策记录(2026-07-07 已定)

1. 门面老 base64/URL 输入路径:**直接删**,不留过渡 fallback。
2. 孤儿输入清理:**靠 janitor TTL** 兜底,new-api 不主动删。
3. input_ref 字段形态:**相对路径列表**(门面前缀 root)。
4. NFSRoot 不变量:门面 / new-api **启动时探测 `inputs/` 可读写,不满足拒绝启动 + log**。
