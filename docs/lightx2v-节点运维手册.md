# LightX2V 计算节点运维手册(lx2v-node.sh)

> 面向操作者的完整手册:新节点接入、gpustack/引擎升级、清理、部署模型、验证、坑与要点。
> 脚本:`docs/scripts/lx2v-node.sh`(仓库)/ `/nfs-models/_transfer/lx2v-node.sh`(NFS 副本)。
> 实测基准:0004/0005 两台全新节点零干预接入,各约 16 分钟(2026-07-06,部署实录 §17.8)。
> 背景与踩坑原始记录:[部署实录](./lightx2v-gpustack-部署实录.md) §17 · [全记录](./lightx2v-20260706-发布部署验证全记录.md)。

---

## 0. 速查卡

```bash
# 新节点接入(先做安全组!):
bash /root/lx2v-node.sh install --token <GPUSTACK_TOKEN>

# 升级本节点的 gpustack worker 镜像(token/卷/IP/server-url 全自动继承):
bash /root/lx2v-node.sh upgrade-gpustack

# 升级本节点的引擎镜像(之后到 UI 逐个删实例重建生效):
bash /root/lx2v-node.sh upgrade-engine                       # lightx2v(默认)
bash /root/lx2v-node.sh upgrade-engine --engine indextts     # IndexTTS-2 语音引擎
bash /root/lx2v-node.sh upgrade-engine --engine acestep      # ACE-Step 文生音乐引擎
bash /root/lx2v-node.sh upgrade-engine --engine vllm-omni    # vLLM-Omni 全模型语音/音频引擎
bash /root/lx2v-node.sh upgrade-engine --engine bernini      # Bernini 视频生成/编辑引擎

# 节点健康速览 / 清理残留:
bash /root/lx2v-node.sh status
bash /root/lx2v-node.sh clean [--purge-data] [--kill-gpu-procs]

# (238 上)出了新包之后,更新 NFS 上的 tar(六镜像)和脚本副本:
bash /root/lx2v-node.sh prepare-transfer
```

> **集群批量**:238 上用 `lx2v-fleet.sh` 对所有 worker 并发跑 node 脚本子命令(自动排除 238 自身):
> `bash /root/lx2v-fleet.sh upgrade-gpustack --offline` / `bash /root/lx2v-fleet.sh -j 3 upgrade-engine --engine vllm-omni --offline` / `bash /root/lx2v-fleet.sh status`。日志在 238 `/tmp/lx2v-fleet/<ip>.log`。
>
> **要一次接入多台全新节点?** 看 **§1.6 批量扩容** —— `lx2v-fleet.sh` 对全新节点用不了(它从 NFS 拉脚本,而新节点还没挂 NFS),必须先 scp + `setup-base` 把 NFS 挂上再让 fleet 接管。
>
> **要做一次现网升级?** 别拼上面这些单条命令 —— 直接整段复制 **§2.5 标准升级流程**,它已按正确顺序串好(打回滚锚 → server → worker → 引擎 → UI 重建),漏一步就可能出事(如回滚锚必须在 `docker pull` 之前打)。

- 所有命令须 **root** 执行,脚本放任意路径均可;
- 全程日志:`/var/log/lx2v-node-<日期>.log`;每步打印 `[step i/N] 时间` 和耗时,长任务(load/pull/save)有进度输出——**长时间无输出再怀疑卡住,先看当前 step 是什么**(toolkit 在线下载 5-8 分钟、引擎 tar load 4-5 分钟都是正常的);
- 任何失败都会打印**原因分析和操作建议**,先照建议做,再看日志。

**当前镜像清单**(六镜像,tag 均须与 gpustack 内置后端注册表 `schemas/inference_backend.py` 的 image_name 一致,勿改名):

| 镜像 | tag | 用途 |
|---|---|---|
| gpustack | `gpustack:lx2v-dev` | server(238,x86)+ worker |
| lightx2v | `lightx2v:arm64-a100-latest` | 图片/视频引擎 |
| indextts2 | `indextts2:arm64-a100-latest` | IndexTTS-2 独立语音引擎(可被 vLLM-Omni 取代,见 §4) |
| acestep | `acestep:arm64-a100-latest` | ACE-Step 文生音乐引擎 |
| vllm-omni | `vllm-omni:arm64-a100-latest` | vLLM-Omni 全模型语音/音频引擎(TTS/AudioX/SoulX/MOSS 等) |
| bernini | `bernini:arm64-a100-latest` | Bernini 原生视频生成/编辑引擎(Bernini-R 渲染器) |

> **2026-07-20 出包**:gpustack 修 `/v2/model-routes` 的 category 校验(放行 `music`,否则 ACE-Step 令 new-api 拿不到模型);vLLM-Omni 修 audiogen 任务 `model` 字段(门面 strip 掉后引擎兜底填 served model,否则 AudioX/SoulX 的 t2a/v2a/v2m/svs 报 400)。**两者都要重出包 + 分别升 server / 换 vllm-omni 引擎 + 重建实例**才生效(见 §2.4)。
>
> 集群升级后请以各节点 `docker images` 实际 ID 为准;`status` 子命令会列出这六镜像 ID 便于比对。

---

## 0.1 命令执行规范(批量操作前必读)

2026-08-20 扩容踩到一次:一条长命令粘进 238 终端被**折行截断**,公钥字符串中间被插进换行 + 缩进,10 台机器的 `authorized_keys` 全写成畸形两行记录 —— 而命令退出码是 0、回显 10 个 `ok`,从输出上完全看不出错(坑 22)。同一天早些时候另一条 `mountpoint -q \n /nfs-output` 也是这么被折断的。因此定下三条:

**① 从 Mac 直接喂脚本给 238,不要往终端粘长命令**

Mac 已免密直连管理节点:`ssh root@111.172.214.42`(公网入口,= 内网 `10.0.0.238`)。多步操作一律走 heredoc —— 引号原样保留,不受终端宽度影响:

```bash
ssh root@111.172.214.42 'bash -s' <<'EOF'
for ip in $(awk 'NF{print $1}' /root/lx2v-new.txt); do
  ssh -n -o BatchMode=yes "root@$ip" '...'
done
EOF
```

`<<'EOF'` 的**引号不能省**:加引号本地 shell 不做任何展开,`$ip`、反引号、单双引号全部原样送到远端;不加引号会在 Mac 上先展开一遍,结果面目全非。

**② 非要在终端里敲,就保持单行且短**(经验值 100 字符以内)。超长内容先落文件再引用,别把它塞进循环体:

```bash
echo 'ssh-ed25519 AAAA...' > /root/mac.pub                # 先写文件
wc -l < /root/mac.pub; ssh-keygen -l -f /root/mac.pub     # 立刻校验:1 行 + 指纹正常
for ip in $(cat /root/lx2v-new.txt); do ssh-copy-id -f -i /root/mac.pub root@$ip; done
```

**③ 写入类操作必须有独立校验步骤,别信退出码**。批量循环里的 `&& echo ok` 只说明命令跑完了,不说明写对了:

| 写了什么 | 怎么验 |
|---|---|
| 公钥 | `ssh-keygen -l -f <file>` 出指纹;再真连一次 `ssh -o BatchMode=yes` |
| 节点清单 | `grep -c .` 核行数 + `grep -vE '^[0-9.]+$'` 查畸形行 |
| authorized_keys | `cat -A` 看行尾 `$`,合法 key 必须**一行一条**、以 `ssh-` 开头且字段数 ≥ 3 |
| worker 是否真接入 | 不看容器 `Up`,看服务端下发的 `registered with worker_id <N>` |

---

## 1. 新节点接入(install)

### 1.1 接入前必做(两件事,顺序无所谓)

1. **☠️ 安全组(最大坑,先做)**:华为云控制台把新节点安全组改成既有 GPU 节点同款 —— **`newapi` 组,不是新机默认的 `hcso` 组**。不做的症状极具迷惑性:脚本全绿、worker 注册成功、本机 healthz OK,但 **UI 永远不转 Ready**——server 到 worker 的 TCP 10150 被安全组拦(ping 是通的,更迷惑)。改完 238 下一轮探测即转 Ready,无需重装。
2. **GPU 驱动确认**:`nvidia-smi` 能列出 4 张 A100(华为云 A100 机镜像通常自带;重装过系统的要先补驱动,脚本第 1 步会拦住)。

### 1.2 分发脚本到新节点

新节点还没挂 NFS,从 238 用内网 scp:

```bash
# 238 上:
scp /root/lx2v-node.sh root@<新节点内网IP>:/root/
```

(238 上的 `/root/lx2v-node.sh` 与 NFS `_transfer/` 里的一份都来自仓库 `docs/scripts/`;仓库更新后跑一次 `prepare-transfer` 会自动同步 NFS 副本。)

### 1.3 执行

```bash
# 新节点上:
bash /root/lx2v-node.sh install \
  --token gpustack_5f53ff2bca9e612f_378048d9c12910eb3bae715ee0ea6e81
```

token 是**集群级注册令牌,所有 worker 复用同一个**;忘了就在任一已有节点上取:
`docker inspect gpustack-worker --format '{{range .Config.Env}}{{println .}}{{end}}' | grep GPUSTACK_TOKEN`

可选参数:
- `--worker-ip <IP>`:多网卡/非 10.x 网段机器显式指定(默认取第一个 10.x 地址,步骤 2 会打印出来,不对就 Ctrl+C 重来);
- `--offline`:严格离线,tar 缺失直接失败不回退在线拉;
- `--clean-residue`:残留扫描发现孤儿引擎容器时一并硬杀;
- `--force`:检测到**同 token** 的现有 worker 时才需要(见 1.5)。

### 1.4 十二个步骤与耗时参照(1-8 为早期实测;9-11 为六镜像后新增)

| step | 内容 | 参考耗时 | 说明 |
|---|---|---|---|
| 1 | GPU 驱动/架构预检 | 5s | 没驱动在这里就停 |
| 2 | 残留扫描 + worker IP 解析 | 5s | IP 定不下来**秒级失败**,不浪费后面时间 |
| 3 | apt:docker.io、nfs-common | ~1.5min | 逐个装(apt 一包失败会整体中止的坑已规避) |
| 4 | 挂 NFS + `/data`、`/nfs-data` 软链 | 秒级 | fstab 两行 + mount |
| 5 | nvidia-container-toolkit | **5-8min** | 源配置来自 NFS `nvidia-repo/`,deb 包从 nvidia.github.io 在线下载(慢是网络,不是卡死) |
| 6 | gpustack 镜像(NFS tar load) | ~1.5min | 4.4G |
| 7 | lightx2v 引擎镜像 | ~4min | 9.8G |
| 8 | indextts2 引擎镜像 | ~4min | ~10G;TTS 整卡单实例可落任意空闲卡 |
| 9 | acestep 引擎镜像 | ~4min | ~8G;文生音乐整卡单实例,全节点预载 |
| 10 | bernini 引擎镜像 | ~4min | ~10G;原生视频生成/编辑,已注册为内置后端,全节点预载 |
| 11 | vllm-omni 引擎镜像(**soft**) | ~4min | ~10G;有 tar 则装、缺则告警不阻塞 install(全模型语音/音频引擎) |
| 12 | 起 worker + 注册/healthz 验证 | ~2min | 旧容器(如有)到这一步才移除,前面失败节点仍有原 worker |

> 上表是**单台**参照(约 16 分钟)。2026-08-20 十台 `-j 10` 并发实测总耗时也是约 15 分钟 —— NFS 读带宽够,10 台同时 load 52G 没有明显互抢;15 台以上仍按 60 卡那次经验降到 `-j 3`。
> 若机器出厂镜像已带 docker + toolkit,step 3/5 会被跳过(`setup-base` 可短到 47 秒),这是正常的,不是没装上 —— 用 `docker info | grep nvidia` 复核。

**成功标志**:`Worker dev-gpustack-a100-000N registered with worker_id N` + `本机 healthz OK`,UI Resources → Workers 转 **Ready**。

### 1.5 残留环境(装过又卸载/被清空的机器)

install 第 2 步自动扫描并按场景处理:

| 发现 | 行为 |
|---|---|
| 旧 worker,**token 与本次相同** | **中止**并提示:换镜像该用 `upgrade-gpustack`;确要重建(容器损坏)加 `--force`。防误跑伤健康节点 |
| 旧 worker,**token 不同** | 判定为接入新 server(典型:管理节点重装丢数据,老 token 已失效、worker "假 Up 真失联")→ 自动移除重建 |
| 孤儿引擎实例容器 | 默认只列出;确认本机不再被原 server 管理时加 `--clean-residue` 硬杀 |
| `gpustack-data` 卷 | **默认复用**(同集群重接入保留 worker 身份);要全新注册先 `clean --purge-data` |
| GPU 野进程 | 只列出永不自动杀(见 §5 要点 7) |

**管理节点(238)重装后的恢复路径**:新 server 建集群拿**新 token** → 每台计算节点 `clean --clean-residue 语义的 clean` + `install --token <新token>` → UI 重新部署模型。

### 1.6 批量扩容(多台全新节点一次接入)

> 2026-08-13 实测:10 台全新 aarch64 4×A100(驱动 570.86.10)一次接入,清单 30 → 40 台。
> 2026-08-20 复测:同样 10 台一次接入,清单 40 → **50 台(200 卡)**,`OK=10 FAIL=0`。这批机器出厂镜像已自带 docker + nvidia-container-toolkit,`setup-base` 只跑 **47 秒**(toolkit 整步被跳过,不是没装成);`install` 18:11 起、18:26 完,约 **15 分钟**,十台步进齐平无掉队。
>
> **本节的命令请照 §0.1 的方式执行**(heredoc 喂给 238,或保持单行)。2026-08-20 那次 `authorized_keys` 写坏,就是 §1.7 ① 的推公钥命令被终端折断造成的。

**核心约束:`lx2v-fleet.sh` 用不了全新节点**——它的机制是 ssh 进去后从 `/nfs-models/_transfer/` 拉最新脚本再执行,而全新节点还没挂 NFS,必然报
`cp: cannot stat '/nfs-models/_transfer/lx2v-node.sh': No such file or directory`。
挂 NFS 又需要 `nfs-common`(只能 apt 装),顺序倒不过来。所以**先手工 scp + `setup-base` 把 NFS 挂上,fleet 才能接管**。

顺序如下,①–⑥ 在 238 上执行(⑦ 起的命令已写成从 Mac 用 heredoc 喂给 238 的形式,在 238 上直接跑 `EOF` 之间的内容也一样)。

**① 安全组(先做,见 §5 坑 1/15)** + 新节点 IP 清单 `/root/lx2v-new.txt`(一行一个)。

**② 免密**

```bash
ls -l ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519   # 已有别重生成,会废掉现有节点免密
NEW=$(grep -vE '^\s*(#|$)' /root/lx2v-new.txt | awk '{print $1}')
for ip in $NEW; do ssh-copy-id -o StrictHostKeyChecking=accept-new "root@$ip"; done
```

**③ 摸底(架构最关键)**

```bash
for ip in $NEW; do
  printf '%-14s ' "$ip"
  ssh -n -o BatchMode=yes "root@$ip" 'echo "$(uname -m) | gpu=$(nvidia-smi -L 2>/dev/null | wc -l) drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"'
done
```

必须是 `aarch64` + 驱动与既有节点一致(当前 570.x,cu128 就是为它锁的)。x86 进不了这套集群,镜像全是 `arm64-a100` tag。

**④ 分发脚本 + `setup-base`**(装 docker/nfs-common/toolkit、挂 NFS,不入集群)

```bash
for ip in $NEW; do scp -o ConnectTimeout=10 /root/lx2v-node.sh "root@$ip:/root/"; done
mkdir -p /tmp/lx2v-bootstrap
for ip in $NEW; do
  ssh -n -o BatchMode=yes "root@$ip" 'bash /root/lx2v-node.sh setup-base' \
    > /tmp/lx2v-bootstrap/$ip.log 2>&1 &
done
wait
for ip in $NEW; do printf '%-14s ' "$ip"; tail -n1 /tmp/lx2v-bootstrap/$ip.log; done
```

> **`ssh -n` 不能省**(见 §5 坑 19)。这步 10 台并发约 10 分钟,瓶颈是 toolkit 的在线 deb 下载。

**⑤ 确认 NFS 挂上,fleet 即可接管**

```bash
bash /root/lx2v-fleet.sh -f /root/lx2v-new.txt status     # 期望 OK=N,且每台 /nfs-models + /nfs-output OK
```

**⑥ 入集群(fleet 批量 install)**——这是最重的一步,每台从 NFS load 六镜像约 52G

```bash
read -rsp 'GPUSTACK_TOKEN: ' TOKEN; echo
nohup bash /root/lx2v-fleet.sh -f /root/lx2v-new.txt -j 10 \
  install --token "$TOKEN" --offline > /tmp/lx2v-install-new.log 2>&1 &
tail -f /tmp/lx2v-install-new.log
```

并发取值:**10 台 `-j 10` 实测一次通过**;15 台以上按 60 卡那次经验降到 `-j 3`,否则同时从 NFS load 大 tar 会抢死带宽。用 `nohup` 挂后台,ssh 断了不影响。

**⑦ 验收 + 合并清单**

三项一起查(镜像数 / worker 容器 / **238 到节点的 10150 连通性**):

```bash
ssh root@111.172.214.42 'bash -s' <<'EOF'
for ip in $(awk 'NF{print $1}' /root/lx2v-new.txt); do
  printf '%-14s ' "$ip"
  imgs=$(ssh -n -o BatchMode=yes "root@$ip" 'docker images --format "{{.Repository}}" | grep -cE "gpustack|lightx2v|indextts2|acestep|vllm-omni|bernini"')
  wk=$(ssh -n -o BatchMode=yes "root@$ip" 'docker ps --filter name=gpustack-worker --format "{{.Status}}" | head -1 | cut -d" " -f1-2')
  hz=$(curl -sf --max-time 4 "http://$ip:10150/healthz" >/dev/null 2>&1 && echo 通 || echo 不通)
  echo "镜像=$imgs  worker=$wk  238→10150=$hz"
done
EOF
```

期望 `镜像=6  worker=Up ..  238→10150=通`(vllm-omni 是 soft 预载,5 也可接受)。
**`238→10150=通` 是安全组的唯一硬证据** —— 节点自己的 `curl 127.0.0.1:10150/healthz` 永远是通的,证明不了任何事;必须从 238 打过去。全通就不会踩坑 1/15。

再确认服务端真的认了(`worker_id` 由 server 下发,比容器 `Up` 可信):

```bash
ssh root@111.172.214.42 'for ip in $(awk "NF{print \$1}" /root/lx2v-new.txt); do printf "%-14s " "$ip"; grep -o "registered with worker_id [0-9]*" /tmp/lx2v-fleet/$ip.log | tail -1; done'
```

合并清单:

```bash
ssh root@111.172.214.42 'bash -s' <<'EOF'
cp /root/lx2v-nodes.txt /root/lx2v-nodes.txt.bak.$(date +%F)
awk 'NF{print $1}' /root/lx2v-nodes.txt /root/lx2v-new.txt | sort -u -o /root/lx2v-nodes.txt
echo "合并后行数: $(grep -c . /root/lx2v-nodes.txt)"
echo "畸形行(应无输出):"; grep -vE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' /root/lx2v-nodes.txt
EOF
```

合并**必须用 `awk` 不能用 `cat >>`**(见 §5 坑 21)。最后到 UI Workers 页确认新节点全部 Ready ——
worker 容器 Up ≠ 注册成功,server 端才是最终事实。

> `docs/scripts/lx2v-nodes.txt` 在 `.gitignore` 里(环境数据不入库),同步一份到本地仓库即可,不用也不能 commit:
> `ssh -n root@111.172.214.42 'cat /root/lx2v-nodes.txt' > docs/scripts/lx2v-nodes.txt`

### 1.7 扩容收尾:Mac 侧免密 + ssh 别名

新节点默认只对 238 免密(§1.6 ② 做的),Mac 上还进不去。Mac 走跳板 `111.172.214.16` + 每台一个 NAT 端口,别名规则见 `~/.ssh/config` 的 `# >>> gpustack-a100 (auto-generated) >>>` 段:`dev-gpustack-a100-00NN` / `gpuNN`,尾行注释内网 IP。

**① 借 238 把 Mac 公钥装到新节点**(238 已免密,不用输密码):

```bash
ssh root@111.172.214.42 'bash -s' <<'EOF'
printf '%s\n' 'ssh-ed25519 AAAA...你的公钥... mac' > /root/mac.pub
ssh-keygen -l -f /root/mac.pub          # 校验:出指纹才算写对
for ip in $(awk 'NF{print $1}' /root/lx2v-new.txt); do
  ssh-copy-id -f -i /root/mac.pub -o BatchMode=yes "root@$ip" </dev/null && echo "ok $ip"
done
EOF
```

用 `ssh-copy-id` 而不是 `echo >> authorized_keys`:前者自己处理换行,后者一旦命令被折行就写出畸形两行记录(坑 22)。已经写坏了的先清:
`ssh -n root@$ip "awk 'NF>2&&/^ssh-/' ~/.ssh/authorized_keys > /tmp/a; mv /tmp/a ~/.ssh/authorized_keys"`。

**② 实测 NAT 端口,不要顺推**。端口由跳板机的 DNAT 规则定,**历史上跳过号**(0025=43047,0026 直接跳到 43051)。逐个端口连进去读内网 IP 才是准的:

```bash
for p in $(seq <起> <止>); do
  ip=$(ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       -o ConnectTimeout=8 -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 \
       -p $p root@111.172.214.16 'hostname -I | tr " " "\n" | grep -m1 "^10\."' 2>/dev/null)
  echo "$p -> ${ip:-拒绝}"
done
```

注意 `nc -z` 探端口**没用** —— NAT 网关对任意端口都回 open,必须真 ssh 进去。
连不上时看清报错:`Permission denied (publickey)` = 端口对、公钥没装(回 ①);`Connection refused/timeout` = 端口没映射。

**③ 端口复用过旧机器 → 先清 known_hosts**(坑 23)。回收的机器会把端口留给新机,老 host key 还在本地,症状是 `Host key verification failed`:

```bash
for p in $(seq <起> <止>); do ssh-keygen -R "[111.172.214.16]:$p"; done
```

**④ 写 config**,格式照抄既有条目,追加在 `# <<< gpustack-a100 (auto-generated) <<<` 之前;改前先 `cp ~/.ssh/config ~/.ssh/config.bak.$(date +%F)`。

**⑤ 严格模式验收**(`StrictHostKeyChecking=yes`,能挡住 host key 不一致):

```bash
for n in $(seq 41 50); do printf 'gpu%-3s ' $n; ssh -n -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 gpu$n 'hostname'; done
```

机器自报的 hostname(`dev-gpustack-a100-0041`)和你编的号能对上,才说明编号没错位。

> **2026-08-20 实测结果**:0041–0050 → 端口 43066–43075(这次恰好连号),内网 IP 依次
> `10.0.0.230 / .77 / .41 / .121 / .87 / .80 / .176 / .66 / .63 / .25`,与 `lx2v-new.txt` 顺序一致。

### 1.8 常量的唯一事实源(防手册与脚本漂移)

手册正文为了可读写的是**具体值**,但这些值的权威定义在脚本顶部的"可调配置"段。**两边不一致时以脚本为准**,并回来改手册。

`docs/scripts/lx2v-node.sh`:

| 手册/命令里出现的值 | 脚本变量 |
|---|---|
| `http://10.0.0.238` | `SERVER_URL` |
| `crpi-…cn-shanghai.personal.cr.aliyuncs.com/reputationly` | `REGISTRY` |
| 六个镜像 tag(§0 清单) | `GPUSTACK_IMAGE` / `ENGINE_IMAGE` / `INDEXTTS_IMAGE` / `ACESTEP_IMAGE` / `BERNINI_IMAGE` / `VLLM_OMNI_IMAGE` |
| `100.125.40.2` + `/share-LLM`、`/share-output` | `NFS_SERVER` / `NFS_MODELS_EXPORT` / `NFS_OUTPUT_EXPORT` |
| `/nfs-models/_transfer/*.tar`、`nvidia-repo/` | `TRANSFER_DIR` + 各 `*_TAR`、`NVIDIA_REPO_DIR` |
| `gpustack-worker`、`10150` | `WORKER_NAME` / `WORKER_PORT` |

`docs/scripts/lx2v-fleet.sh`:

| 值 | 脚本变量 |
|---|---|
| `/root/lx2v-nodes.txt`(默认清单) | `NODES_FILE`(`-f` 可覆盖) |
| 默认并发 5 | `JOBS`(`-j` 可覆盖) |
| `10.0.0.238`(自动排除的 manager) | `SELF_IP` |
| `/nfs-models/_transfer/lx2v-node.sh` | `NFS_SCRIPT`(fleet 每次执行前从这里拉最新脚本) |

**不在脚本里的**:跳板机 `111.172.214.16` 与各节点 NAT 端口、别名 `gpuNN` —— 唯一事实源是 Mac 的 `~/.ssh/config`(§1.7),脚本和集群都不知道它们的存在。238 的公网入口 `111.172.214.42` 同理。

---

## 2. 升级

### 2.1 升级 gpustack worker 镜像(upgrade-gpustack)

**何时用**:gpustack 仓出了新包(ACR 的 `lx2v-dev` 浮动 tag 更新)之后,每台计算节点跑一次。

```bash
bash /root/lx2v-node.sh upgrade-gpustack            # 在线增量拉(有旧镜像时只拉变更层,分钟级)
bash /root/lx2v-node.sh upgrade-gpustack --offline  # 或从 NFS tar load(需先在 238 prepare-transfer)
```

**自动继承,不会弄丢**:旧容器的全部 `GPUSTACK_*` 环境变量、数据卷(含匿名卷,按真实卷名原样复用)、`--server-url`、`--worker-ip`。缺标准 env(老版本 UI 命令只带 TOKEN)会按默认值补齐并告警。worker 重启期间**引擎实例容器不受影响**(独立容器,worker 起来后重新接管)。

**server(238)不用这个脚本**——它是 x86 且参数不同,按全记录 §3.1 的三条命令手工升级(pull → stop/rm → 原参数 run;数据卷是 `gpustack-data`,迁移自动跑)。

### 2.2 升级引擎镜像(upgrade-engine)

**何时用**:某个引擎仓出了新包之后(LightX2V profiles/launcher、index-tts、acestep、vllm-omni)。

```bash
bash /root/lx2v-node.sh upgrade-engine                       # lightx2v(默认),打印 旧ID -> 新ID
bash /root/lx2v-node.sh upgrade-engine --engine indextts     # IndexTTS-2 语音引擎
bash /root/lx2v-node.sh upgrade-engine --engine acestep      # ACE-Step 文生音乐引擎
bash /root/lx2v-node.sh upgrade-engine --engine vllm-omni    # vLLM-Omni 全模型语音/音频引擎
```

各引擎镜像 tag 必须与 gpustack 内置后端注册表(`schemas/inference_backend.py` 的 image_name)完全一致——worker 按名匹配本地镜像。全 worker 批量:`bash /root/lx2v-fleet.sh -j 3 upgrade-engine --engine <名> --offline`(大 tar 降并发)。

**⚠️ 关键**:换镜像**不影响正在运行的实例**(它们锁旧镜像 ID)。生效方式:UI → Instance List → **逐个删除实例**让其自动重建(先删一个、等新的 Running 再删下一个,服务不断)。

### 2.3 出新包后 238 侧的配套动作(prepare-transfer)

```bash
# 238 上,任一镜像出新包后:
bash /root/lx2v-node.sh prepare-transfer
```

做四件事:拉**六镜像**(gpustack / lightx2v / indextts2 / acestep / bernini / vllm-omni)arm64 变体 → 按 digest 变化 save 到 NFS 六个 tar(未变的跳过;带写入进度,`.tmp`+`mv` 防半截)→ **x86 机器上自动把本地 gpustack tag 拉回 amd64**(否则 238 之后重建 server 容器会 exec format error——坑 §5.5)→ 把脚本自身同步到 `_transfer/`。vllm-omni 是 soft:拉不到只告警、不阻塞其余 tar。

### 2.4 一次完整现网升级(以 2026-07-20 gpustack + vllm-omni 为例)

只动变了的镜像,顺序 **server 先 → worker → 重建实例**:

```bash
# ① 238:刷 NFS tar(只有变了的 gpustack/vllm-omni 会重存,其余 digest 跳过)
bash /root/lx2v-node.sh prepare-transfer

# ② 238 server 手动升级(脚本不管 x86 server;详见下方"server 不用脚本")
docker pull crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:lx2v-dev
docker stop gpustack-server && docker rm gpustack-server
docker run -d --name gpustack-server --restart unless-stopped -p 80:80 \
  --volume gpustack-data:/var/lib/gpustack --volume /nfs-output:/nfs-output \
  crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:lx2v-dev \
  --system-default-container-registry quay.io
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/   # 200

# ③ 全 worker 升 gpustack(fleet)
bash /root/lx2v-fleet.sh upgrade-gpustack --offline

# ④ 全 worker 换 vllm-omni 引擎(fleet;lightx2v/indextts/acestep 没变不用动)
bash /root/lx2v-fleet.sh -j 3 upgrade-engine --engine vllm-omni --offline
```

**⑤ UI 重建实例**:vLLMOmni 系(audiox/soulx/indextts-2/qwen3-tts/moss-*)逐个删实例重建到新引擎镜像;ACE-Step(ACEStep 后端,镜像没变)不用动。**server 必须先于 worker**(版本校验软 + DB/API 方向 server ≥ worker)。

### 2.5 标准升级流程(通用模板,直接复制)

§2.4 是一次具体升级的实录;本节是**不绑定镜像/日期**的模板,升级时整段复制,只改两处:第 ⑤ 步列哪些引擎、第 ⑦ 步重建哪些实例。

**前置**:先确认要升的包**已经构建完成**(各仓 GitHub Actions → 对应 workflow 显示 success),否则 `prepare-transfer` 拉到的还是旧镜像,后面全白做。各仓出包流水线都是 `workflow_dispatch` 手动触发,push 不会自动构建。

以下全部在 **manager(238)** 上执行:

```bash
# ① 打回滚锚 —— 必须是**第一步**!
#    lx2v-dev 是浮动 tag,任何 pull 之后旧镜像就没有名字了、回滚就无从谈起。
#    注意 prepare-transfer 自己就会 pull(它拉 arm64 六镜像,x86 上还会再拉一次
#    amd64 把本地 tag 恢复回来,见 lx2v-node.sh 的 cmd_prepare_transfer),
#    所以这一步必须排在 prepare-transfer **前面**,不是仅仅排在 ③ 的 pull 前面。
docker tag crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:lx2v-dev \
  gpustack-rollback:pre-$(date +%Y%m%d)
docker images gpustack-rollback      # 确认锚已建再往下走

# ② 刷 NFS tar(拉六镜像,digest 没变的自动跳过)
bash /root/lx2v-node.sh prepare-transfer

# ③ 升 server —— 必须先于 worker(版本校验软 + DB/API 方向 server ≥ worker)
docker pull crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:lx2v-dev
docker stop gpustack-server && docker rm gpustack-server
docker run -d --name gpustack-server --restart unless-stopped -p 80:80 \
  --volume gpustack-data:/var/lib/gpustack --volume /nfs-output:/nfs-output \
  crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/gpustack:lx2v-dev \
  --system-default-container-registry quay.io
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/    # 期望 200

# ④ 全 worker 升 gpustack
bash /root/lx2v-fleet.sh -j 10 upgrade-gpustack --offline

# ⑤ 全 worker 换引擎镜像 —— 只列这次变了的,没变的别动(改这里)
bash /root/lx2v-fleet.sh -j 10 upgrade-engine --engine lightx2v --offline
bash /root/lx2v-fleet.sh -j 10 upgrade-engine --engine vllm-omni --offline
# 可选:indextts / acestep / bernini

# ⑥ 巡检:worker 状态、六镜像 ID、NFS 挂载、每卡显存、实例容器
bash /root/lx2v-fleet.sh -j 10 status
```

**⑦ UI 重建实例**(改这里):Instance List 里把**用到了新引擎镜像的实例**逐个删除让其自动重建——**先删一个、等新的 Running 再删下一个**,服务不断。换了镜像但没重建实例 = 新代码根本没上线(实例锁旧镜像 ID)。

**回滚 server**(①的锚派上用场时):

```bash
docker stop gpustack-server && docker rm gpustack-server
docker run -d --name gpustack-server --restart unless-stopped -p 80:80 \
  --volume gpustack-data:/var/lib/gpustack --volume /nfs-output:/nfs-output \
  gpustack-rollback:pre-<锚的日期> \
  --system-default-container-registry quay.io
```

数据库迁移设计为**向后兼容**(只加表/加枚举值、不改旧表),旧镜像跑新库无碍,所以回滚只需退镜像,不用动 `gpustack-data` 卷。

**要点**

- **并发 `-j 5`** 是吞吐与 NFS 带宽的折中。离线模式下每台都要从 NFS load 约 10G 的 tar,并发太高会互相抢带宽。若 `/tmp/lx2v-fleet/<ip>.log` 出现 load 变慢/超时/个别节点失败,降到 `-j 3` 重跑即可——**升级是幂等的**,重复执行无副作用。
- 节点清单默认读 `/root/lx2v-nodes.txt`,manager 自身会自动排除;换清单用 `-f`。
- 任一节点失败则整体退出码非 0,逐台日志在 `/tmp/lx2v-fleet/<ip>.log`。
- 只升引擎、gpustack 没变时,②③④ 可整段跳过,直接从 ⑤ 开始。

---

## 3. 清理与巡检

```bash
bash /root/lx2v-node.sh status
```
一屏看:worker 容器状态/healthz、六镜像 ID(gpustack/lightx2v/indextts2/acestep/bernini/vllm-omni,与 §0 清单比对)、NFS 挂载、每卡显存、引擎实例容器(按 runtime label 精确识别,含 -init/-unhealthy-restart)。

```bash
bash /root/lx2v-node.sh clean                     # 删 worker + 硬杀全部引擎实例容器(kill+sleep+rm -f)
bash /root/lx2v-node.sh clean --purge-data        # 追加删 gpustack-data 卷(worker 身份清零)
bash /root/lx2v-node.sh clean --kill-gpu-procs    # 追加 kill -9 GPU 上全部进程(钝器,先看清单再加)
```

镜像默认全保留(重装可增量复用)。若本机历史上跑过**匿名卷**,老数据可能在 64 位 hash 卷里,`docker volume ls` 逐个确认后再清。

---

## 4. 部署后操作:上模型(UI)

### 4.1 模型部署参数速查

统一部分:Source=Local Path,Version=Auto,Scheduling=Manual 勾卡;Backend 视模型:图片/视频=**LightX2V**,语音(旧路径)=**IndexTTS**。**语音/音频/音乐的 vLLM-Omni 系 + ACE-Step 见 §4.1a**(IndexTTS-2 已可改走 vLLMOmni 取代独立 IndexTTS)。

| 模型 | Model Path(`/nfs-models/wuhanjisuan894/` 下) | Backend | GPUs/Replica | Category | Backend Parameters | 显存/加载参考 |
|---|---|---|---|---|---|---|
| z-image(t2i) | `models/Z-Image-Turbo` | LightX2V | **1** | Image | 无 | ~20G,秒级出图 |
| wan2.2-t2v | `models/Wan-AI/Wan2.2-T2V-A14B` | LightX2V | **4** | Video | 无 | ~35G/卡,载入~60s,720p/81帧~60s |
| wan2.2-i2v | **同上 T2V 目录**(I2V 无独立基座) | LightX2V | **4** | Video | **`--model-cls wan2.2_moe_distill` + `--task i2v`(必填)** | ~33G/卡,720p~87s |
| qwen-image-edit(i2i) | `models/Qwen-Image-Edit-2511` | LightX2V | **1** | Image | 无(路径含 Edit 自动选型) | ~20G,载入~86s,热态~22-38s |
| qwen-image(t2i,未上) | `models/Qwen-Image`(以实际为准) | LightX2V | **1** | Image | `--task t2i` | 热态~17s |
| **IndexTTS-2(tts)** | `models/IndexTTS-2`(权重目录,含 config.yaml/gpt.pth/s2mel.pth/qwen0.6bemo4-merge 等,来源 `indextts2-checkpoints.tar.gz` 解压) | **IndexTTS** | **1** | 自动打 text_to_speech | **无(会被忽略并告警——引擎全部 env 配置,如 `INDEXTTS_MAX_QUEUE`)** | 空载 ~8-10G 但**整卡预订**(长文本峰值设计);载入 ~1-2min,`/ready` 200 才 Running |

**部署口诀**:
- **单卡模型显式设 GPUs per Replica=1**——Manual 勾 N 卡 + M 副本时调度器按 N÷M 分卡,勾 2 卡 1 副本会分成 2 卡/实例,launcher 找不到 2 卡变体直接失败(坑 17-8,防呆按设计工作);
- 多卡模型(wan 系)设 =4,或"勾整机 4 卡 ÷ 1 副本"让除法自然等于 4;
- Backend Parameters 每框写 `--key value`(空格分隔即可,后端会正确拆分);
- qwen 系有**主机内存红线**:每实例 ~60G Shmem(调度器只看显存,看不见这个),**单节点 ≤2 副本**(3 绝对上限,第 4 个 OOM 整机);多副本**错峰启动**(先 1 副本,Running 后再加);
- 兼容性提示 "N×40 GiB VRAM" = 整卡预订,是正确表现。

### 4.1a vLLM-Omni 语音/音频 + ACE-Step 音乐模型部署(2026-07-20 实测)

模型权重根:**`/nfs-models/wuhanjisuan894/vllm-omni-speech/`**(IndexTTS-2 例外,在 `.../models/IndexTTS-2`)。Source=Local Path,Scheduling=Manual,Category 见下(vLLMOmni 系=Text-to-Speech,ACE-Step=Music)。

| 模型(能力) | Model Path(相对 `/nfs-models/wuhanjisuan894/`) | Backend | 卡/副本 | Backend Parameters | Env |
|---|---|---|---|---|---|
| IndexTTS-2(情感合成) | `models/IndexTTS-2` | vLLMOmni | 1(2-stage 同卡) | `--deploy-config <见下坑A> --allowed-local-media-path /nfs-output` | `HF_HOME=.../models/IndexTTS-2/hf_cache` |
| Qwen3-TTS(语音合成/克隆) | `vllm-omni-speech/Qwen3-TTS-1.7B-CustomVoice` | vLLMOmni | 1 | `--allowed-local-media-path /nfs-output` | `HF_HOME=.../vllm-omni-speech/hf_cache` |
| MOSS-TTSD(双人对话) | `vllm-omni-speech/MOSS-TTSD-v1.0` | vLLMOmni | **2** | `--deploy-config .../vllm-omni-speech/moss_ttsd_a100_40g.yaml --allowed-local-media-path /nfs-output` | `HF_HOME=.../vllm-omni-speech/hf_cache` |
| MOSS-VoiceGenerator(声音设计) | `vllm-omni-speech/MOSS-VoiceGenerator` | vLLMOmni | **2** | `--deploy-config .../vllm-omni-speech/moss_voicegen_a100_40g.yaml` | `HF_HOME=.../vllm-omni-speech/hf_cache` |
| AudioX(文生音效/视频配音效/配乐) | `vllm-omni-speech/AudioX` | vLLMOmni | 1 | `--model-class-name AudioXPipeline --allowed-local-media-path /nfs-output`(**不带** trust-remote-code) | `HF_HOME=.../vllm-omni-speech/hf_cache` |
| SoulX-Singer(歌声合成) | `vllm-omni-speech/SoulX-Singer` | vLLMOmni | 1 | `--enforce-eager --deploy-config /deploy-configs/soulxsinger_svs.yaml --allowed-local-media-path /nfs-output` | `HF_HOME=.../vllm-omni-speech/hf_cache` |
| ACE-Step(文生音乐/改编/重绘) | `vllm-omni-speech/ACE-Step-1.5` | **ACEStep** | 1 | **无**(env 全自动注入) | **无** |

后端自动注入、不用手填:`--omni`、host/port、`--trust-remote-code`(有 `--model-class-name` 时自动跳过)、`DIFFUSION_ATTENTION_BACKEND=FLASH_ATTN`、`HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE=1`。ACEStep 后端更自洽(`ACESTEP_CHECKPOINTS_DIR`=Model Path 等全自动)。

**vLLMOmni 部署五个坑(按杀伤力)**:

- **坑 A · IndexTTS-2 deploy-config 路径要改**:模型自带 `indextts2-a100.yaml` 用的是 POC 挂载前缀 `/models/IndexTTS-2/...`,GPUStack 里模型在 `/nfs-models/...`,直接用会去 HF 下 tokenizer → 离线 `LocalEntryNotFoundError`。修:`sed 's#/models/IndexTTS-2/#/nfs-models/wuhanjisuan894/models/IndexTTS-2/#g' indextts2-a100.yaml > indextts2-a100-gpustack.yaml`,`--deploy-config` 指修正版。**凡自带 deploy-config 的模型都先 grep 里面的绝对路径**。
- **坑 B · HF_HOME 各指各的 cache**:MOSS 系用共享 `vllm-omni-speech/hf_cache`(含 MOSS-Audio-Tokenizer codec);IndexTTS-2 用它自己的 `models/IndexTTS-2/hf_cache`(含 w2v-bert/bigvgan)。指错 → codec/tokenizer 离线加载失败崩。
- **坑 C · 读输入的必须 `--allowed-local-media-path /nfs-output`**:AudioX v2a/v2m(视频)、SoulX(参考音)、MOSS-TTSD(参考音)、Qwen3/IndexTTS 克隆——不加则门面注入的 `file://` 被 vLLM MediaConnector 拒(HTTP 400)。`GPUSTACK_MEDIA_ROOT` 未设时后端 fail-closed 不注入,故直接在 Backend Parameters 手填(用户显式值优先)。
- **坑 D · custom backend 的 Manual 只能单机**:vLLMOmni 是 custom backend,Manual GPU 选卡**只支持单个 worker**;多副本要铺到多台 → 报 `Manual GPU selection across multiple workers is not supported`。单副本单机用 Manual,**多副本跨机用 Auto**(整卡独占,调度器自动填空卡)。
- **坑 E · ACE-Step 首启从 ModelScope 下载**:`HF_HUB_OFFLINE` 不 gate ModelScope,ACEStep 起来会拉一个 ~8G 组件。能下完就 Running,但依赖 MS 网络 + 可能缓存到临时目录每次重下。后续优化:预填 NFS cache 或设 `MODELSCOPE_OFFLINE`。

**部署前置(否则上面玩法静默 400/崩)**:worker env 已带 `GPUSTACK_EXTRA_MOUNTS=/nfs-models,/nfs-output,/nfs-data`;离线 HF cache(t5/clip/whisper/MOSS-codec)在各自 hf_cache;deploy-config yaml 在 NFS 或镜像 `/deploy-configs/`(SoulX/MOSS 在镜像里,IndexTTS 在模型目录)。

### 4.2 部署验证三板斧

```bash
# ① launcher 选型(实例容器日志第一行,最重要的验收点):
docker logs $(docker ps --format '{{.Names}}' | grep <模型名> | grep run) 2>&1 | grep lx2v-launcher | head -2
# 必须命中预期 profile,如: model_cls=wan2.2_moe_distill gpus=4 profile=wan2.2-i2v/int8-4card
# IndexTTS 实例无 launcher,验收点是模型加载完成 + /ready 转 200:
#   docker logs <实例容器> 2>&1 | grep -E "task worker started|Uvicorn running" | head -2
#   curl -s -o /dev/null -w '%{http_code}\n' http://<节点IP>:<实例端口>/ready   # 200=就绪,503=加载中

# ② 资源就位:watch nvidia-smi 看显存爬到参考值;qwen 系再看主机内存:
grep -E 'Shmem|MemAvailable' /proc/meminfo    # 别用 free 的 used 列,不计 Shmem 会严重低估

# ③ 端到端冒烟(238 上,$KEY 为 All models 权限的 API key):
# 文生(t2v/t2i,无输入文件,可直连门面):
curl -s -X POST http://10.0.0.238/v1/videos -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"<模型名>","task_type":"t2v","prompt":"...","target_video_length":81,"user_id":1}'
# → 轮询: GET /v1/videos/<task_id>;done 后: GET /v1/videos/<task_id>/content 下载
#
# ⚠️ 带输入文件的任务(i2i/i2v 的图、tts 的参考音)门面已改走 input_refs:
#   raw base64/URL 字段会被 400 拒(NFS input 改造后契约,见 lightx2v-nfs-input-design.md)。
#   直连门面冒烟需先把输入手工放到 /nfs-output/inputs/<task_type>-<模型>/YYYY/MM/DD/<user_id>/
#   再在请求里传 input_refs 相对路径——繁琐,**推荐直接走 new-api(体验区或 API)冒烟**,
#   由 new-api 自动物化输入。tts 示例(new-api 侧):
#   POST /pg/videos {"model":"<TTS模型>","prompt":"你好","metadata":{"task_type":"tts","voice":"<base64 wav>"}}
```

产物路径约定:输出 `/nfs-output/<task_type>-<模型名>/YYYY/MM/DD/<user_id>/<task_id>.{png,mp4,wav}`;输入(经 new-api 物化)在 `/nfs-output/inputs/<同结构>/<gid>-<字段>.{png,wav}`。

---

## 5. 坑与要点汇总(按杀伤力排序)

| # | 坑/要点 | 现象 | 处置 |
|---|---|---|---|
| 1 | **云安全组不同组** | 脚本全绿、注册成功、本机 healthz OK,但 UI 永不 Ready;238 上 ping 通、`curl <ip>:10150` 超时 | 新节点**接入前**加入既有节点同款安全组 |
| 2 | **单卡模型没设 GPUs per Replica** | 实例日志 `No 2-GPU variant for model_cls 'qwen_image'` | 显式设 1;见 §4.1 口诀 |
| 3 | **wan-i2v 忘加后端参数** | launcher 报路径暗示 t2v 与变体不符(fail-loudly) | 补 `--model-cls wan2.2_moe_distill --task i2v` |
| 4 | **匿名卷**(历史容器没带 -v 起的) | 升级换命名卷后"数据全没了" | 数据没丢,在 hash 卷里;`docker inspect <容器> --format '{{json .Mounts}}'` 先看真实卷名。脚本 upgrade 已自动继承,手工操作才会踩 |
| 5 | **x86 机器拉过 `--platform arm64` 的同名 tag** | 之后本地 `docker run` 报 exec format error | `docker pull <镜像>`(不带 platform)拉回本机架构;prepare-transfer 已自动处理 |
| 6 | **管理页 download 置灰** | curl 提交后不轮询,任务停在 assigned | 非 bug:poll-on-GET 设计,`GET /v1/videos/<id>` 一次即推进(sweeper 10min 兜底) |
| 7 | **GPU 野进程** | GPUStack 看不见外部显存占用,照常调度 → 实例 OOM | 部署/接入前 `nvidia-smi` 清场;`clean --kill-gpu-procs` 或人工 kill -9 |
| 8 | **qwen Shmem 红线** | 第 3-4 个副本起来后整机 OOM-kill | 单节点 ≤2 副本;量内存用 `/proc/meminfo` 的 Shmem,别信 free |
| 9 | wan 帧数必须 **4n+1** | 引擎拒绝或产物异常 | target_video_length 用 81/121/161(720p 上限 161) |
| 10 | **prompt 带引号** | qwen 系任务失败或行为怪异 | 引号须 JSON 转义(程序侧 json.dumps;手测避免引号) |
| 11 | apt 一次装多包 | 一个包找不到,**其余包也全没装上** | 脚本已逐个装;手工操作注意 |
| 12 | `docker save -o` | 失败留下隐藏半截文件,load 报 unexpected EOF | 用 `>` 重定向;脚本已用 `.tmp`+`mv` |
| 13 | toolkit 不在 Ubuntu 源 | `Unable to locate package nvidia-container-toolkit` | 源两件套在 NFS `_transfer/nvidia-repo/`,脚本自动使用 |
| 14 | 引擎升级后实例没变化 | 换了镜像但行为还是旧的 | 实例锁旧镜像 ID,UI 逐个删实例重建才生效 |
| 15 | **新节点默认 `hcso` 安全组** | 同坑1(脚本全绿但 UI 不 Ready,238 curl 10150 超时) | 华为云控制台把新节点安全组从 `hcso` 改成 **`newapi`**(既有 GPU 节点同组);改完 238 下一轮探测即转 Ready,无需重装 |
| 16 | **new-api 拿不到全部模型** | 渠道"智能获取"少模型;server 日志 `Invalid category: music` @ `/v2/model-routes` | ACE-Step 的 `music` category 撞旧校验白名单。**升级到 2026-07-20 gpustack 包**(`model_routes.py` 改对 `CategoryEnum` 校验);临时可在渠道**手填 `ace-step`**(推理走门面不经此接口) |
| 17 | **AudioX/SoulX 报 `Field required: model`(400)** | new-api 文生音效等 audiogen 玩法提交即 400 | 门面 strip 掉 `model`,旧引擎 `AudioGenTaskRequest.model` 必填。**升级到 2026-07-20 vllm-omni 包**(引擎兜底填 served model)+ 重建 audiox/soulx 实例 |
| 18 | **vLLMOmni 多副本 Manual 跨机报错** | `Manual GPU selection across multiple workers is not supported for custom backends` | custom backend 的 Manual 只支持单机;多副本跨机改 **Auto**(见 §4.1a 坑 D) |
| 19 | **交互 shell 里 `ssh ... &` 被 Stopped** | 一批后台 ssh 全变 `Stopped`,`wait` 报 `job N stopped`;`kill` 打不死(要 `kill -9`) | 后台 ssh 读 tty stdin 触发 `SIGTTIN`。**加 `ssh -n`**。`lx2v-fleet.sh` 没这问题是因为脚本(非交互 shell)的后台任务 stdin 自动接 `/dev/null` |
| 20 | **杀了 ssh,远端还在跑** | 上面那批 ssh 被 kill 后,远端 `pgrep -al apt` 仍见 `apt-get install nvidia-container-toolkit` | ssh 无 tty 时远端进程变孤儿继续执行。**别急着重跑**——打断 apt 会留 dpkg 半配置状态;等它跑完再重跑(`setup-base`/`install` 都幂等) |
| 21 | **`cat >>` 合并节点清单粘连** | `lx2v-nodes.txt` 里出现 `10.0.0.4710.0.0.236` 这种畸形行,两个 IP 同时丢失 | 源文件末尾无换行符所致。用 `awk 'NF{print $1}' a.txt b.txt \| sort -u` 合并(awk 按记录读,不受影响);合并后 `grep -c .` 核行数 |
| 22 | **长命令粘进终端被折行截断** | 退出码 0、回显一片 `ok`,但结果是错的。实例:公钥被折成两行写进 `authorized_keys`(`cat -A` 可见第二行以空格开头),ssh 一直 `Permission denied`;另一例 `mountpoint -q` 与参数被拆开,报 `bad usage` + `/nfs-output: Is a directory` | 见 **§0.1**:多步操作走 `ssh 238 'bash -s' <<'EOF'`,终端里只敲短单行;写公钥用 `ssh-copy-id -f -i`(自己处理换行)而非 `echo >>`;**每个写入操作配独立校验**,别信 `&& echo ok` |
| 23 | **NAT 端口复用导致 host key 失效** | Mac 上新配的别名 `ssh gpu41` 报 `Host key verification failed`;端口是通的、公钥也装了 | 回收的机器把 NAT 端口留给了新机,本地 `known_hosts` 还存着老机器的 key。清掉重采:`ssh-keygen -R "[111.172.214.16]:<端口>"`。另注意 **`nc -z` 探不出真假**(NAT 网关对任意端口都回 open),判端口只能真 ssh 进去读 `hostname -I` |

## 6. 脚本自身的升级

仓库 `docs/scripts/lx2v-node.sh` 有改动后:Mac `scp` 到 238 `/root/` → 238 跑一次 `prepare-transfer`(自动同步 NFS 副本)→ 各节点直接用 `/nfs-models/_transfer/lx2v-node.sh` 或重新 scp。脚本改动**必须先过检视再 commit**(仓库规矩)。
