#!/usr/bin/env python3
"""把 GPUStack 引擎实例滚到当前引擎镜像上(在 manager 上跑)。

为什么需要它
------------
`upgrade-engine` 只把新镜像拉到节点,**正在跑的实例不会自己换镜像** —— 它们锁的是
创建时解析出来的旧 image ID。手册 §2.2 的四步就是这个原因:

1. 先确认节点上 tag 已指向新 ID(tag 没变时重建回来的还是旧镜像);
2. **在该实例所在节点停掉旧容器** —— 只删 GPUStack 里的实例**不会**停容器(worker
   重启后不接管既有容器),旧容器继续占显存,新实例必 CUDA OOM;
3. 再删实例,让调度器重建;
4. 等新实例真的能服务(`/v1/models` 200)**并核对镜像 ID** 再动下一个。

手抄这四步容易漏第 2 步,而且"重建成功"很难判断。这个脚本把它固化成一条命令:
按 worker 串行(同机多实例共享显存)、跨机并行,逐台打印"before/after 镜像"。

用法(manager 上)
-----------------
    python3 lx2v-rebuild-instances.py --backend vLLMOmni --dry-run   # 先看计划
    python3 lx2v-rebuild-instances.py --backend vLLMOmni             # 执行
    python3 lx2v-rebuild-instances.py --models tts2 --max-parallel 2 # 只滚一个模型
    python3 lx2v-rebuild-instances.py --backend vLLMOmni --expect-image-id cf9e6f8d6237

必须缩小范围(--backend 或 --models),否则拒绝执行:它会真的删实例。

认证:默认从本机 gpustack-server 容器里读 jwt_secret_key 现签一个短命 admin JWT
(不落盘);也可用 --api-key 或环境变量 GPUSTACK_API_KEY。

退出码:0 全部已是新镜像;1 有实例没滚成功;2 参数不足以缩小范围。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API_BASE = "http://127.0.0.1"
SERVER_CONTAINER = "gpustack-server"
DEFAULT_ENGINE_REF = "crpi-xzr81d0490mc3794.cn-shanghai.personal.cr.aliyuncs.com/reputationly/vllm-omni:arm64-a100-latest"
LOAD_TIMEOUT_S = 1800
POLL_S = 20


# --------------------------------------------------------------------------- auth / api
def mint_jwt(container: str = SERVER_CONTAINER) -> str:
    """从本机 server 容器里现签一个 admin JWT(不落盘)。"""
    code = (
        "from datetime import timedelta;"
        "from gpustack.security import JWTManager;"
        "print(JWTManager(secret_key=open('/var/lib/gpustack/jwt_secret_key').read().strip(),"
        "expires_delta=timedelta(minutes=120)).create_jwt_token('admin'))"
    )
    out = subprocess.run(
        ["docker", "exec", container, "python3", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    token = out.stdout.strip()
    if out.returncode != 0 or not token:
        raise SystemExit(
            "拿不到 admin JWT(是不是不在 manager 上?):\n"
            + (out.stderr or out.stdout).strip()
        )
    return token


class Api:
    def __init__(self, token: str, api_key: str | None = None):
        self.cookie = f"gpustack_session={token}"
        self.api_key = api_key

    def _open(self, path: str, method: str = "GET"):
        req = urllib.request.Request(API_BASE + path, method=method)
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        else:
            req.add_header("Cookie", self.cookie)
        return urllib.request.urlopen(req, timeout=60)

    def get(self, path: str):
        with self._open(path) as r:
            return json.loads(r.read())

    def delete(self, path: str) -> int:
        try:
            with self._open(path, "DELETE") as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def instances(self) -> list[dict]:
        out, page = [], 1
        while True:
            d = self.get(f"/v2/model-instances?page={page}&perPage=100")
            out += d.get("items", [])
            if page >= d["pagination"]["totalPage"]:
                return out
            page += 1


# --------------------------------------------------------------------------- node helpers
def ssh(ip: str, cmd: str, timeout: int = 120) -> str:
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"root@{ip}", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (r.stdout or r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001 — 单台失败不该中断整批
        return f"<ssh failed: {exc}>"


def container_line(ip: str, name: str) -> str:
    return ssh(
        ip,
        "docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}' "
        f"| grep -F {name}-run-0 | head -1",
    )


def tag_image_id(ip: str, ref: str) -> str:
    """节点上这个 tag 当前指向的镜像 ID —— 判断"该不该滚"的唯一正确依据。"""
    return ssh(ip, f"docker images --format '{{{{.ID}}}}' {ref} | head -1")


def stop_container(ip: str, name: str) -> str:
    return ssh(
        ip,
        f"docker stop {name}-run-0 >/dev/null 2>&1; "
        f"docker rm {name}-run-0 >/dev/null 2>&1; echo ok",
    )


def serves(ip: str, port: int) -> bool:
    out = ssh(
        ip,
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 8 http://{ip}:{port}/v1/models",
    )
    return out == "200"


# --------------------------------------------------------------------------- rollout
class Roller:
    def __init__(self, api: Api, engine_ref: str, expect_id: str | None, dry_run: bool):
        self.api = api
        self.engine_ref = engine_ref
        self.expect_id = expect_id
        self.dry_run = dry_run
        self.lock = threading.Lock()
        self.results: list[tuple[str, str, str]] = []

    def record(
        self, out: list[str], instance: str, outcome: str, detail: str = ""
    ) -> None:
        with self.lock:
            self.results.append((instance, outcome, detail))
            print(f"[{outcome}] {instance} {detail}".rstrip(), flush=True)

    def roll_one(self, inst: dict) -> None:
        name = inst["name"]
        lines = [
            f"\n== {name} ({inst['model_name']}) @ {inst['worker_name']} ({inst['worker_ip']})"
        ]
        try:
            outcome, detail = self._roll(inst, lines)
        except Exception as exc:  # noqa: BLE001
            outcome, detail = "FAIL", f"异常: {exc}"
        # 一次性输出:4 个线程并发时,逐行 print 会让 header 和结论错位成别人的实例名
        print("\n".join(lines), flush=True)
        self.record(lines, name, outcome, detail)

    def _roll(self, inst: dict, lines: list[str]) -> tuple[str, str]:
        name, mid, iid, ip = (
            inst["name"],
            inst["model_id"],
            inst["id"],
            inst["worker_ip"],
        )
        lines.append("   before: " + container_line(ip, name))

        want = self.expect_id or tag_image_id(ip, self.engine_ref)
        if not want:
            return "SKIP", f"节点上没有 {self.engine_ref}(先 upgrade-engine)"
        if want in container_line(ip, name):
            return "SKIP", f"已是新镜像 {want}"
        if self.dry_run:
            return "WOULD", f"需要重建 -> {want}"

        # ① 停旧容器:不先停,新实例会因为显存被占而 CUDA OOM
        lines.append("   stop old container: " + stop_container(ip, name))

        # ② 删记录 → 调度器重建
        code = self.api.delete(f"/v2/model-instances/{iid}")
        if code not in (200, 204):
            return "FAIL", f"DELETE 返回 {code}"

        # ③ 等**这台机器上**的替代实例能服务(按 worker_ip 认领,避免认成别人的)
        before = {i["id"] for i in self.api.instances() if i["model_id"] == mid}
        deadline = time.time() + LOAD_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(POLL_S)
            for cand in self.api.instances():
                if cand["model_id"] != mid or cand["id"] in before:
                    continue
                if cand["worker_ip"] != ip:
                    continue
                port = (cand.get("ports") or [None])[0]
                if not port or not serves(ip, port):
                    continue
                line = container_line(ip, cand["name"])
                if want not in line:
                    return "FAIL", f"新实例镜像不是 {want}: {line}"
                lines.append(f"   after: {line}")
                return "OK", f"新实例 {cand['id']} 镜像 {want}"
        return "FAIL", f"{LOAD_TIMEOUT_S}s 内没有在 {ip} 上就绪的替代实例"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把 GPUStack 实例滚到当前引擎镜像上(见模块 docstring)"
    )
    ap.add_argument(
        "--backend", help="只滚这个后端的实例(如 vLLMOmni);不给就必须给 --models"
    )
    ap.add_argument("--models", help="逗号分隔的模型名,只滚这些")
    ap.add_argument(
        "--engine-ref", default=DEFAULT_ENGINE_REF, help="节点上用来判定'新镜像'的 tag"
    )
    ap.add_argument(
        "--expect-image-id", help="显式指定期望镜像 ID(默认用节点上 tag 指向的 ID)"
    )
    ap.add_argument(
        "--max-parallel", type=int, default=4, help="跨机并行度(同机永远串行)"
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印计划,不动实例")
    ap.add_argument("--api-key", help="GPUSTACK API key(默认现签 admin JWT)")
    args = ap.parse_args()

    if not args.backend and not args.models:
        print(
            "必须缩小范围:给 --backend(如 vLLMOmni)或 --models <名字,...>。\n"
            "  先跑 --dry-run 看计划,确认后再执行(这个脚本会真的删实例)。",
            file=sys.stderr,
        )
        return 2

    api = Api("", args.api_key) if args.api_key else Api(mint_jwt())
    instances = api.instances()
    if args.backend:
        instances = [i for i in instances if i.get("backend") == args.backend]
    if args.models:
        wanted = {m.strip() for m in args.models.split(",") if m.strip()}
        instances = [i for i in instances if i["model_name"] in wanted]
    instances.sort(key=lambda i: (i["model_name"], i["id"]))
    if not instances:
        print(f"没有匹配的实例(backend={args.backend}, models={args.models})")
        return 0

    print(f"目标 {len(instances)} 个实例(backend={args.backend or '-'}):")
    for i in instances:
        print(
            f"  {i['id']:<6} {i['model_name']:<22} {i['state']:<9} "
            f"{i['worker_name']} ({i['worker_ip']})"
        )

    roller = Roller(api, args.engine_ref, args.expect_image_id, args.dry_run)
    sem = threading.Semaphore(max(1, args.max_parallel))
    per_worker: dict[str, threading.Lock] = {}

    def run(inst: dict) -> None:
        with sem:
            # 同一台机器上的多个实例共享显存,必须串行
            with per_worker.setdefault(inst["worker_ip"], threading.Lock()):
                roller.roll_one(inst)

    threads = [threading.Thread(target=run, args=(i,)) for i in instances]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    def count(outcome: str) -> int:
        return sum(1 for r in roller.results if r[1] == outcome)

    fail = [r for r in roller.results if r[1] == "FAIL"]
    print(
        f"\n==> 汇总: OK={count('OK')}  SKIP={count('SKIP')}  "
        f"WOULD={count('WOULD')}  FAIL={len(fail)}  (共 {len(roller.results)})"
    )
    for name, _, detail in fail:
        print(f"    FAIL {name}: {detail}")
    if args.dry_run:
        return 0
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
