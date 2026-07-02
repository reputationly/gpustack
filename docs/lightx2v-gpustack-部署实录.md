# LightX2V 接入 GPUStack 部署实录(Z-Image 首套跑通)

> 记录 2026-07-02 首套环境从零到「Z-Image 在 GPUStack 里出图」的完整操作步骤与踩坑。
> 配套设计见 [`lightx2v-backend-design.md`](lightx2v-backend-design.md)。
> 一句话结论:**LightX2V 以 GPUStack Custom 后端接入,单卡 bf16 跑 Z-Image,全链路闭环成功。**

---

## 0. 环境

| | GPUStack Server | A100 Worker |
|---|---|---|
| 主机名 | `dev-gpustack-manager` | `dev-gpustack-a100-0001` |
| 内网 IP | `10.0.0.238` | `10.0.0.163` |
| 架构 | **x86_64** | **aarch64(鲲鹏 ARM)** |
| 系统 | Ubuntu 22.04.3 | Ubuntu 22.04.3 |
| CPU/内存 | 104 核 / 376Gi | 128 核 / **251Gi** |
| GPU | 无 | **4× A100-PCIE-40GB**,驱动 570.86.10 / CUDA 12.8 |
| 磁盘 | 913G 可用 | 897G 可用 |

- **混合架构**:server 走 x86 镜像、worker 走 arm64 镜像;GPUStack 原生支持异构集群。
- UI 公网入口:`http://111.172.214.42`(内网 `http://10.0.0.238` 浏览器打不开)。
- SFS(NFS)服务器 `100.125.40.2`,两个共享:`/share-LLM`(20T,模型)、`/share-output`(1T,产出)。

### 网络关键事实(决定所有下载策略)
- ❌ **Docker Hub 不通**(`registry-1.docker.io` 超时)、`get.docker.com` 无法解析、github.com 超时。
- ✅ **可达**:`mirrors.aliyun.com`(apt,x86 用 `ubuntu`、ARM 用 `ubuntu-ports`)、`quay.io`、华为云镜像、**阿里云 ACR** `crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/`(已设 public 免登录拉取)、`nvidia.github.io`(toolkit apt 源)。
- 结论:apt 走 aliyun;容器镜像走 quay/ACR;**大镜像 163 直拉不稳,用 238 拉→存 NFS→163 load**(见 §6)。

---

## 1. 装 Docker(两台)

```bash
apt-get update
apt install docker.io          # 29.1.3,走 aliyun apt 源;get.docker.com 不通就用它
systemctl enable --now docker
docker version
```

## 2. 装 nvidia-container-toolkit(仅 A100 worker)

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update && apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
# 验收:容器内看到 4 张 A100
docker run --rm --gpus all --entrypoint nvidia-smi <任一镜像> -L
```

## 3. 挂载 SFS(两台)

> **绝不自建 NFS,用华为云 SFS**。命名用连字符 + 语义化(不用 `/nfs-ro`/`/nfs-rw`,因为盘其实可写)。

```bash
apt-get install -y nfs-common
mkdir -p /nfs-models /nfs-output
# 写进 fstab 持久化(重启自动挂 + 新节点拷这两行即复用)
tee -a /etc/fstab <<'EOF'
100.125.40.2:/share-LLM     /nfs-models   nfs   vers=3,timeo=600,nolock,noresvport,_netdev   0 0
100.125.40.2:/share-output  /nfs-output   nfs   vers=3,timeo=600,nolock,noresvport,_netdev   0 0
EOF
mount -a && df -h | grep nfs
```
- `noresvport`:华为 SFS 官方推荐,断线重连更稳。`_netdev`:开机等网络就绪再挂。
- **实测速度(fio,`--iodepth=16`)**:读 ~1GB/s、写 ~150MB/s(单队列 iodepth=1 只有读 540/写 150)。对图片/视频小文件足够。

## 4. GPUStack Server(`dev-gpustack-manager`)

```bash
docker run -d --name gpustack-server --restart unless-stopped -p 80:80 \
  --volume gpustack-data:/var/lib/gpustack \
  crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:latest \
  --system-default-container-registry quay.io
# 拿初始密码
docker exec gpustack-server cat /var/lib/gpustack/initial_admin_password
```
- 镜像多架构、server/worker 同镜像;`--system-default-container-registry quay.io` 让内部组件拉 quay(Docker Hub 不通)。
- 浏览器开 `http://111.172.214.42`,`admin` + 密码,首登设新密码。

## 5. Worker 加入(`dev-gpustack-a100-0001`)

UI:**Resources → Clusters → Add Cluster(选 Docker)** → 进 cluster → **Add Worker/添加节点** → 第 3 步「指定参数」填:
- **节点 IP** = `10.0.0.163`
- **额外卷挂载** = `/nfs-models`(关键:这是把 NFS 模型挂进**推理容器**的正规入口;worker 自己的 `-v` 不会传给推理容器)
- 数据卷 `gpustack-data`、容器名 `gpustack-worker` 默认

UI 第 4 步生成命令后,**改三处再跑**(别直接用):

```bash
# retag 复用本地已有镜像,避免 163 走 quay 拉 v2.2.0(不稳)
docker tag quay.io/gpustack/gpustack:latest quay.io/gpustack/gpustack:v2.2.0

docker run -d --name gpustack-worker \
  -e "GPUSTACK_TOKEN=gpustack_xxxxx" \
  --restart=unless-stopped --privileged --network=host \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --volume gpustack-data:/var/lib/gpustack \
  --volume /nfs-models:/nfs-models \
  --volume /nfs-output:/nfs-output \
  --runtime nvidia \
  quay.io/gpustack/gpustack:v2.2.0 \
  --server-url http://10.0.0.238 \    # ← 改成内网,别用 UI 给的公网 111.172.214.42
  --worker-ip 10.0.0.163
```
- 三处改动:**server-url 公网→内网**、加 `/nfs-output` 挂载、镜像 retag 复用。
- 日志 `Worker ... registered with worker_id 1` + UI 节点页显示 4 张 A100 = 上线。

## 6. 引擎镜像分发(238 拉 → NFS → 163 load)

163 直拉 ACR 的大 lightx2v 镜像(29GB)会中途超时。改用 238 转运:

```bash
# ① 238:x86 主机也能拉 arm64 变体
IMG=crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/lightx2v:arm64-a100-latest
docker pull --platform linux/arm64 "$IMG"
# ② 238:save 到 NFS —— 必须用 > 重定向,不能用 -o(见坑#5)
mkdir -p /nfs-output/_transfer
nohup sh -c "docker save '$IMG' > /nfs-output/_transfer/lightx2v-arm64.tar" &   # nohup 防断线
# ③ 163:从 NFS load
docker load -i /nfs-output/_transfer/lightx2v-arm64.tar
docker images | grep lightx2v
```
> 长期方案:238 上架私有 `registry:2`,各节点走内网拉,免 save/load。

## 7. LightX2V 现成工作区 + 路径软链

`/nfs-models/wuhanjisuan894/` 是前团队在同款硬件调通的完整工作区(引擎镜像、模型、配置、启动脚本、实测报告全在)。**模型/配置/脚本全硬编码 `/data/...`**(旧 NFS 挂在 `/data`),做软链让老路径生效:

```bash
ln -sfn /nfs-models/wuhanjisuan894 /data        # 脚本/config 里的 /data/... 全部解析
ln -sfn /nfs-models/wuhanjisuan894 /nfs-data    # 部分 smoke 脚本硬编码 /nfs-data
```
- 模型:`/nfs-models/wuhanjisuan894/models/Z-Image-Turbo`(bf16 完整仓 ~31G)。
- 关键配置(`z_image_bf16_single.json`,**三个必改项**,见坑#6):
```json
{ "aspect_ratio":"1:1","num_channels_latents":16,"infer_steps":9,
  "attn_type":"sage_attn2","rope_type":"torch",
  "enable_cfg":false,"sample_guide_scale":0.0,"patch_size":2 }
```

## 8. Standalone 冒烟(先脱离 GPUStack 验证引擎)

```bash
docker run -d --name zimg-smoke --gpus all -e CUDA_VISIBLE_DEVICES=0 --memory=240g \
  -p 8000:8000 -v /data:/data -e PYTHONPATH=/opt/LightX2V \
  "$IMG" python -m lightx2v.server --model_cls z_image --task t2i \
  --model_path /data/models/Z-Image-Turbo --config_json /data/lightx2v_configs/z_image_bf16_single.json \
  --host 0.0.0.0 --port 8000
# 等 /health=200,POST /v1/tasks/image/(body 传 prompt/save_result_path/seed/aspect_ratio)→ 轮询 /v1/tasks/{id}/status → completed
docker rm -f zimg-smoke     # ★ 验证完必须删!否则占着 GPU 0,GPUStack 部署会 OOM(坑#7)
```
- API:`POST /v1/tasks/image/` → 轮询 `GET /v1/tasks/{id}/status` 到 `completed`,产物写 `save_result_path`。
- aspect_ratio 按**请求体**传(config 里的只是默认);`1:1`→1328×1328,默认 16:9 因转置 bug 出 928×1664 竖图。

## 9. 注册 Custom 后端(UI:Model Service → Inference Backends → Add Backend → Custom)

| 字段 | 值 |
|---|---|
| Name | `lightx2v`(UI 自动补 `-custom` → `lightx2v-custom`,**必须 `-custom` 结尾**) |
| Health Check Path | `/health` |
| **Default Execution Command** | **留空!**(env 别填这里,见坑#8) |
| Default Environment Variables | `PYTHONPATH=/opt/LightX2V`、`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` |
| Version | `arm64-a100-latest`(设为 Default) |
| Image Name | `crpi-....../reputationly/lightx2v:arm64-a100-latest` |
| Framework | **CUDA**(NVIDIA) |
| Execution Command | 见下 |

```
python -m lightx2v.server --model_cls z_image --task t2i --model_path {{model_path}} --config_json /nfs-models/wuhanjisuan894/lightx2v_configs/z_image_bf16_single.json --host 0.0.0.0 --port {{port}}
```
- 占位符:`{{model_path}}`、`{{port}}` 部署时自动替换(还有 `{{worker_ip}}`、`{{model_name}}`)。

## 10. 部署 Z-Image(Deployments → Deploy Model)

| 字段 | 值 |
|---|---|
| Name | `z-image` |
| Source | Local Path |
| Model Path | `/nfs-models/wuhanjisuan894/models/Z-Image-Turbo` |
| Cluster | `a100-cluster` |
| Backend | `lightx2v` |
| Replicas | `1`(先验证,后扩 4) |
| Advanced → Model Category | `image` |
| Advanced → **Enable Generic Proxy** | ✅ 勾上(LightX2V 用原生异步 API,标准 image 路由不匹配) |
| Scheduling → Placement | Spread(扩副本时分散到不同卡) |

- **Compatibility Check** 估 30.58 GiB VRAM < 40G → 自动调度到**单张 A100**(GPU 数不用手填)。
- 状态 → **Running**(health `/health` 通过)。实测:加载 ~12s,首图冷态 ~19s,后续热态 ~7.6s/张。
- 验证出图:`curl POST http://localhost:40041/v1/tasks/image/`(实例端口 40041,save 路径用 `/nfs-models/...`)→ completed → 2.1MB / 1328×1328 PNG。✅

---

## 坑汇总(按遇到顺序)

| # | 坑 | 现象 | 修法 |
|---|---|---|---|
| 1 | **Docker Hub 全不通** | `get.docker.com` 无法解析、`docker pull` 超时 | apt 装 `docker.io`;镜像走 quay/ACR;`--system-default-container-registry quay.io` |
| 2 | **163 直拉大镜像不稳** | quay/ACR 拉 lightx2v 中途 `connection reset/timed out` | 238 拉 → `docker save` → NFS → 163 `docker load`(§6);长期上私有 registry |
| 3 | **混合架构** | server x86 / worker ARM | GPUStack 原生支持;引擎镜像必须 arm64;238 拉 arm64 用 `docker pull --platform linux/arm64` |
| 4 | **fstab 顺序坑** | 先跑 `umount + mount -a` 验证但没先写 fstab → NFS 被卸载没挂回 | 先写 fstab 行,再 `mount -a` |
| 5 | **`docker save -o` 失败** | `-o file` 方式 `Exit 1`、文件不出现 | 改 **`docker save IMG > file`**(shell 立刻建文件、能看增长);配 `nohup` 防断线 |
| 6 | **z_image 两个必崩项** | 默认 `flash_attn3`(Hopper 专属,A100 崩)、默认 rope `flashinfer`(镜像没装,`NoneType not callable`) | config 改 `attn_type=sage_attn2`、`rope_type=torch` |
| 7 | **GPUStack 部署 OOM** | `CUDA out of memory`,但本进程只用 18G / 卡快满 | standalone 冒烟容器 `zimg-smoke` 没删,占着 GPU 0 ~21G。`docker rm -f zimg-smoke` 后 Auto-Restart 自动重载成功。**教训:standalone 验完必删容器** |
| 8 | **env 填错字段** | 环境变量填进了「Default Execution Command」 | Execution Command 留空,env 放「Default Environment Variables」的 Add Variable |
| 9 | **worker server-url 用公网** | UI 生成命令里 `--server-url` 是公网 IP | 改成内网 `http://10.0.0.238`,worker 不绕公网 |
| 10 | **aspect_ratio 转置 + 默认非 1:1** | config 写 1:1 仍出 928×1664 竖图 | aspect_ratio 按**请求体**传;要 16:9 横图填 `9:16`(runner 转置 bug),`1:1` 不受影响 |
| 11 | **LightX2V 不自动建目录** | `save_result_path` 父目录不存在 → `FileNotFoundError`、任务 failed | **调用方(dispatcher/new-api)设置 save 路径前必须先 `mkdir -p` 父目录**;或引擎侧加 `os.makedirs(dirname, exist_ok=True)`(Phase A 先在调用方建) |
| 12 | **worker 的 `-v` 会复制给推理容器** | 只在 UI「额外卷挂载」填了 `/nfs-models`,但推理容器里 `/nfs-output` 也在 | GPUStack 把 worker 容器的 `-v` 挂载复制给推理容器;所以 worker 命令里加 `-v /nfs-output:/nfs-output` 即可,不必重登 worker |
| 13 | **多卡 Custom 后端 overcommit** | Wan `torchrun --nproc_per_node=4` 要 4 卡,但 GPUStack 估只需 15G→只想给 1 卡;Scheduling Auto 下手选 4 卡报「resource overcommit / Unable to find schedulable worker」 | **overcommit 只是警告,非阻塞**:Scheduling Mode 改 **Manual** → 手动勾该节点 4 张卡 → 直接 Save,GPUStack 照办(自定义后端无 TP 感知,靠手选卡) |
| 14 | **Wan 被自动标成 LLM 类别** | Model Category=Auto 时 GPUStack 把 wan 猜成 LLM | 不影响出视频,只是标签;可在部署 Advanced 里显式选 video |
| 15 | **配置内部路径 `/data` 在推理容器不存在** | 前团队 wan 配置内部写死 `/data/models/...`,GPUStack 推理容器只挂 `/nfs-models` 无 `/data` | 复制一份配置、把内部 ckpt 路径改成 `/nfs-models/...` 绝对路径(z_image/wan 都用 /nfs-models) |
| 16 | **curl `-d` JSON 被粘贴换行截断** | 多行 `-d` 粘贴时字符串里混入换行 → JSON 坏 → 返回无 task_id | `-d '{...}'` 放**一行**;或先 `echo "$RESP"` 看原始返回排错 |

---

## 关键结论(Z-Image 生产标定,实测报告佐证)
- **生产最优 = bf16 单卡 7.6s/张**;int8 慢 2.9×(A100 无 INT8 算力路径)、z_image 显存宽裕不需要;多卡 ulysses 只 1.2× 且 30 head 不整除 4(4 卡不可用)。
- **吞吐 = N×单卡实例 + 负载均衡**:4 单卡实测 0.53 img/s;内存无忧(高并发只排队不增内存)。
- **必改配置**:`attn_type=sage_attn2` + `rope_type=torch` + `infer_steps=9`。

## 待办(收尾)
1. ✅ **扩到 4 单卡副本**(已完成)——Replicas 1→4 + Spread,4 张 A100 各起一个实例(GPU 0/1/2/3 各 ~20G)。
2. ✅ **`/nfs-output` 挂进推理容器**(已完成)——worker `-v /nfs-output` 已复制给推理容器,产物落 `/nfs-output/t2i-z_image/年月日/user/task.png`(调用方需先 `mkdir -p`,见坑#11)。
3. ✅ **Generic Proxy 正式路由 + 负载均衡**(已完成)——见下。
4. ✅ **Wan2.2 T2V(int8 4卡)第二节点部署**(已完成)——见 §12。
5. 后续:new-api 对接、238 私有 registry、前端体验区、dispatcher(轮询亲和/背压)、Wan i2v/flf2v/s2v 与 720p 长视频。

### #2 Generic Proxy 访问方式(实测)
- 路径:`http://<server>/model/proxy/<route_id>/<原生路径>`。本例 route_id=1(在 **Model Service → Routes** 看),提交 = `http://10.0.0.238/model/proxy/1/v1/tasks/image/`。
- 鉴权:Header `Authorization: Bearer <API_KEY>`(**Access Control → API Keys** 建)。
- 路由:body 里 `"model":"z-image"` 或 Header `X-GPUStack-Model: z-image`(route_id 已绑定,双保险);UI 模板的 `n/size/response_format` 是 OpenAI 格式,**LightX2V 不认,要用它的原生 body**(prompt/save_result_path/...)。
- **实测**:8 个并发提交经网关 → GPUStack 轮询分发给 4 个实例并行 → 8 张图同时落 NFS(~16s),4 张 A100 都被点亮。这就是报告 4×单卡 0.53 img/s 的来源。
- **⚠️ 亲和限制**:proxy 轮询负载均衡,而 LightX2V 任务状态是**各实例进程内存态**——`GET /v1/tasks/{id}/status` 经 proxy 可能被打到别的实例查不到。生产取状态需 **dispatcher(§6)** 绑 task→实例;但**成品落 NFS(save_result_path)后 new-api 直接读文件**,简单场景不必轮询,已绕开该问题。

---

## 12. 第二模型:Wan2.2 T2V(int8 4 卡,第二节点)

设计里**每节点 z_image / wan 二选一**。加一台 A100 节点 `dev-gpustack-a100-0002`(`10.0.0.109`)专跑 Wan,163 继续跑 z-image。

### 12.1 新节点 onboarding(零下载复用)
和 §1–5 完全一样,但镜像**全从 NFS load,不碰 ACR/quay**——这是"新节点秒复用"的兑现:
- Docker + toolkit + `nfs-common`(apt);挂同一套 SFS(fstab 直接拷)+ 软链 `/data`、`/nfs-data` → `/nfs-models/wuhanjisuan894`。
- **引擎镜像**:`docker load -i /nfs-output/_transfer/lightx2v-arm64.tar`(§6 存的 tar 还在,直接 load)。
- **gpustack 镜像**:从 ACR 拉一次 `crpi-.../reputationly/gpustack:latest`(7G,稳)→ retag `quay.io/gpustack/gpustack:v2.2.0`;并**也 `docker save` 到 NFS**(`gpustack-arm64.tar`,1.6G),以后新节点两个镜像全 load,彻底零下载。
- 加 worker:**复用 a100-cluster 的同一个 `GPUSTACK_TOKEN`**(注册令牌可加多 worker),只改 `--worker-ip 10.0.0.109`。worker 日志出现 `lightx2v-custom` 说明 Custom 后端是**集群级、新 worker 自动就有**。

### 12.2 Wan 标定(实验报告 docs/Wan2.2-I2V-实验测试报告.md)
- **生产最优 = int8 4 卡 ulysses**;int8 单卡能跑但慢(A100 无 INT8 算力,价值是省显存);**bf16 多卡必 CPU OOM**(4 rank ×(57G+11G)=276G>256G)→ 想多卡必须 int8。
- 配置:`model_cls=wan2.2_moe`、`task=t2v`、`int8-torchao`、`self/cross_attn=flash_attn2`(Wan 用 flash_attn2,非 sage_attn2)、`rope_type=torch`、`boundary=0.875`(MoE 双专家 high/low)、`seq_p_size=4 ulysses`、`infer_steps=4`、帧=4n+1。
- 分辨率:480p 可长视频(15s+),720p 天花板 161 帧(10s)。实测本机 4 卡加载 ~62s、720p/81帧生成 ~60s,4 卡 95-99% 满载 ~35G。
- 配置文件见 `docs/configs/wan_t2v_int8_4card.json`(路径已改 `/nfs-models`),放到 `/nfs-models/wuhanjisuan894/lightx2v_configs/`。

### 12.3 注册 wan-custom 后端 + 部署
- 后端 `wan-custom`(同 z_image 的填法),**Execution Command 用 torchrun**:
  ```
  torchrun --nproc_per_node=4 --master_port=29524 -m lightx2v.server --model_cls wan2.2_moe --task t2v --model_path {{model_path}} --config_json /nfs-models/wuhanjisuan894/lightx2v_configs/wan_t2v_int8_4card.json --host 0.0.0.0 --port {{port}}
  ```
- 部署:Model Path `/nfs-models/wuhanjisuan894/models/Wan-AI/Wan2.2-T2V-A14B`、Backend `wan`、Replicas 1、**Scheduling Mode = Manual → 手动勾 0002 的 4 张卡**(overcommit 警告非阻塞,见坑#13)、Enable Generic Proxy。
- 结果:torchrun 拉起 4 rank、`device_mesh(seq_p=4)`、health 200 → Running;`POST /v1/tasks/video/`(帧 4n+1)出 720p h264 mp4,4 卡满载。

**至此:z-image(163,4×单卡)+ wan-t2v(0002,1×4卡)两模型同集群生产就绪。**
