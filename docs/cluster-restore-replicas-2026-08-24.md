# 集群重建副本数恢复清单(2026-08-24)

> 背景:2026-08-24 下午为把 0001-0050 宿主机驱动从 570.86.10 升到 580.65.06,
> 关机并删除了全部 51 台 worker 注册记录。`workers` / `model_instances` 两表已清空,
> **`models` 表 27 条定义仍在,但 `replicas` 全被置 0**。
>
> 下面的副本数是删除前(2026-08-24 17:2x)从 `model_instances` 实际统计出来的,
> 数据库里已无此信息,重建时按此表把 `replicas` 调回去。
>
> cluster 未受影响:id=1 `a100-image-video`,registration_token 不变。

## 删除前的运行副本数

| 副本数 | 模型 |
| --- | --- |
| **4** | ace-step、qwen-image、qwen-image-edit、ernie-image-turbo、z-image、seedvr2、minimax-h3-ref2va、minimax-h3-fl2va、minimax-h3-fast-ref2va、minimax-h3-fast-fl2va |
| **3** | hunyuan-image-3、indextts-2、qwen3-tts、moss-voicegen |
| **2** | minimax-h3-base-ref2va、minimax-h3-base-fl2va |
| **0**(当时就没起) | wan2.2-t2v、wan2.2-i2v、wan2.2-flf2v、wan2.2-vace、infinitetalk-480p、infinitetalk-720p、ltx2-v2a、audiox、bernini、soulx-singer、moss-ttsd |

合计 27 个模型,删除前共 16 个模型在跑、约 52 个实例。

## 重建注意

1. **不要一次性把全部 replicas 调回去**。50 台 worker 同时接入 + 27 个模型抢着拉起
   50+ 实例,会同时打 NFS(权重)和 GPU。按引擎分批放,参考既有教训:
   ARM A100 上任何 4 路 offload 并发会整机死机,单机 ≤2 副本、错峰启动。
2. 驱动升级本身不需要动镜像:cu128 镜像在 580 驱动上已实测通过(51 号机
   lightx2v bf16 matmul),cu130 镜像在 570/580 上都通过。
3. 节点接入照旧:`lx2v-node.sh install --token <cluster-1 token> --offline`。
