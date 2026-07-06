import asyncio
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Tuple

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.api.exceptions import (
    BadRequestException,
    InternalServerErrorException,
    NotFoundException,
    ServiceUnavailableException,
)
from gpustack.config.config import get_global_config
from gpustack.gateway.utils import model_instance_prefix, router_header_key
from gpustack.http_proxy.strategies import select_least_pending_instance
from gpustack.schemas.models import ModelInstance, ModelInstanceStateEnum
from gpustack.schemas.video_generation_task import (
    VIDEO_TASK_TERMINAL_STATES,
    VideoGenerationTask,
    VideoTaskStateEnum,
)
from gpustack.schemas.workers import Worker
from gpustack.server.db import async_session
from gpustack.server.deps import CurrentUserDep
from gpustack.server.services import (
    ModelInstanceService,
    ModelRouteService,
    ModelService,
    UserService,
    WorkerService,
)
from gpustack.server.worker_request import request_to_worker

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_OUTPUT_ROOT = "/nfs-output"


def _output_root() -> str:
    """Shared RW NFS output root the engine writes results to and the server
    streams them back from (§7.5). Both the worker (via GPUSTACK_EXTRA_MOUNTS)
    and the server host must bind-mount this same path, so an absolute
    save_result_path under it is visible on both sides.

    Config-driven (default "/nfs-output", env GPUSTACK_LIGHTX2V_OUTPUT_ROOT via
    BaseSettings, editable at runtime via /config → Storage Settings). Config,
    not model/request input — a model deployer must not redirect where results
    are written.
    """
    cfg = get_global_config()
    root = getattr(cfg, "lightx2v_output_root", None) if cfg else None
    return root or _DEFAULT_OUTPUT_ROOT


_SUBMIT_TIMEOUT = 30
_STATUS_TIMEOUT = 15

# Dispatch attempts after the initial one (see VideoTaskBase.retry_count).
# Every re-dispatch — successful or transiently failed — consumes one, so an
# instance that keeps accepting-then-dying can't loop a task forever.
_MAX_DISPATCH_RETRIES = 5

# Engine task actions (§7.2) that go to /v1/tasks/image/ rather than
# /v1/tasks/video/. Everything else (t2v/i2v/flf2v/s2v) is a video task.
_IMAGE_TASK_TYPES = {"t2i", "i2i"}
_VIDEO_TASK_TYPES = {"t2v", "i2v", "flf2v", "s2v"}
# Finite set of accepted actions. task_type is the first path component of the
# §7.2 NFS layout, so it MUST be constrained — an unsanitized value like
# "../../tmp/x" would let save_result_path / input writes escape the output root.
_VALID_TASK_TYPES = _IMAGE_TASK_TYPES | _VIDEO_TASK_TYPES

# Facade input field -> (engine request field, default extension). The facade
# accepts a URL (passed through — the engine downloads it) or base64 bytes,
# which the server persists to NFS and hands the engine a local path (§7.7).
_INPUT_FIELDS = {
    "image": ("image_path", ".png"),
    "last_frame": ("last_frame_path", ".png"),
    "image_mask": ("image_mask_path", ".png"),
    "audio": ("audio_path", ".wav"),
}

# Control keys consumed by the facade; never forwarded verbatim to the engine.
_CONTROL_KEYS = {"model", "task_type", "user_id"} | set(_INPUT_FIELDS.keys())

# Engine-native path fields the facade OWNS. They must never come from the
# request body: input paths are set only by the facade after materializing
# base64/URL inputs to NFS (§7.7 — a raw path from an external caller would let
# an inference user make the worker read arbitrary shared-mount files / other
# tenants' outputs), and save_result_path is dictated by the facade.
_ENGINE_OWNED_FIELDS = {
    "image_path",
    "last_frame_path",
    "image_mask_path",
    "audio_path",
    "video_path",
    "save_result_path",
}

# Engine TaskStatus.value -> our lifecycle state.
_ENGINE_STATE_MAP = {
    "pending": VideoTaskStateEnum.ASSIGNED,
    "processing": VideoTaskStateEnum.RUNNING,
    "completed": VideoTaskStateEnum.DONE,
    "failed": VideoTaskStateEnum.FAILED,
    "cancelled": VideoTaskStateEnum.CANCELED,
}

_DATA_URI_RE = re.compile(r"^data:[^;]*;base64,", re.IGNORECASE)


def _engine_kind(task_type: str) -> str:
    return "image" if task_type in _IMAGE_TASK_TYPES else "video"


def _output_ext(task_type: str) -> str:
    return ".png" if _engine_kind(task_type) == "image" else ".mp4"


def _sanitize(name: str) -> str:
    # Model names may be owner-prefixed ("owner/name"); flatten to one path segment.
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "unknown")


def _rel_path(
    task_type: str, model_name: str, user_id: int, task_id: str, ext: str
) -> str:
    now = datetime.now(timezone.utc)
    return (
        f"{task_type}-{_sanitize(model_name)}/"
        f"{now:%Y/%m/%d}/{user_id}/{task_id}{ext}"
    )


def _is_url(value: str) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _write_bytes(path: str, data: bytes) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _ensure_parent_dir(path: str) -> None:
    # The engine writes save_result_path verbatim and does NOT create the parent
    # (it only pre-creates its own outputs/ dir), so the server — which mounts the
    # same RW NFS — must create the date/user/model dir before the engine writes.
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _authorize_task(task, user) -> None:
    """Only the submitting principal (or an admin) may read a task. 404 (not 403)
    so a task_id learned by another tenant doesn't even confirm existence."""
    if user.is_admin:
        return
    if task.owner_user_id is not None and task.owner_user_id == user.id:
        return
    raise NotFoundException(message="Task not found", is_openai_exception=True)


async def _resolve_target_model(
    session: AsyncSession, request: Request, user, model_name: str
):
    """Resolve the OpenAI-style ``model`` name to a concrete Model, mirroring the
    OpenAI proxy path (auth + weighted route-target selection), minus streaming
    and LoRA handling which video jobs don't use."""
    import random

    if not await UserService(session).model_allowed_for_user(
        model_name=model_name,
        user_id=user.id,
        api_key=getattr(request.state, "api_key", None),
    ):
        raise NotFoundException(message="Model not found", is_openai_exception=True)

    model_route_service = ModelRouteService(session)
    targets = await model_route_service.resolve_route_targets(model_name)
    if not targets:
        raise NotFoundException(
            message="Model not found or no running instances available",
            is_openai_exception=True,
        )
    weights = [t.weight for t in targets]
    target = (
        random.choices(targets, weights=weights, k=1)[0]
        if sum(weights) > 0
        else random.choice(targets)
    )
    model = await ModelService(session).get_by_id(target.model_id)
    if not model:
        raise NotFoundException(message="Model not found", is_openai_exception=True)

    # Mirror openai.py: expose the resolved model/route on request.state so
    # ModelUsageMiddleware can attribute the submission for usage recording.
    request.state.model = model
    model_route = await model_route_service.get_by_name(model_name)
    request.state.model_route_id = model_route.id if model_route else None
    return model


def _instance_headers(instance: ModelInstance) -> Dict[str, str]:
    return {
        router_header_key: f"{model_instance_prefix(instance)}.static",
        "Content-Type": "application/json",
    }


async def _persist_input(
    body: Dict[str, Any],
    task_type: str,
    model_name: str,
    user_id: int,
    task_id: str,
) -> Dict[str, str]:
    """Materialize any base64 inputs onto shared NFS and return the engine-field
    overrides (URLs are passed through untouched)."""
    overrides: Dict[str, str] = {}
    for field, (engine_field, ext) in _INPUT_FIELDS.items():
        value = body.get(field)
        if not value:
            continue
        if _is_url(value):
            overrides[engine_field] = value
            continue
        raw = _DATA_URI_RE.sub("", value) if isinstance(value, str) else value
        try:
            data = base64.b64decode(raw)
        except Exception as e:
            raise BadRequestException(
                message=f"Invalid base64 for input '{field}': {e}",
                is_openai_exception=True,
            )
        rel = f"inputs/{_rel_path(task_type, model_name, user_id, f'{task_id}-{field}', ext)}"
        abs_path = os.path.join(_output_root(), rel)
        await asyncio.to_thread(_write_bytes, abs_path, data)
        overrides[engine_field] = abs_path
    return overrides


async def _parse_video_request(
    request: Request,
) -> Tuple[Dict[str, Any], str, str, int]:
    """Parse and minimally sanity-check the submit body → (body, model_name,
    task_type, user_id). No parameter validation beyond what's needed to route
    (new-api validates upstream)."""
    try:
        body = await request.json()
    except Exception as e:
        raise BadRequestException(
            message=f"Invalid JSON body: {e}", is_openai_exception=True
        )
    if not isinstance(body, dict):
        raise BadRequestException(message="Body must be a JSON object")
    model_name = body.get("model")
    if not model_name:
        raise BadRequestException(
            message="Missing 'model' field", is_openai_exception=True
        )
    task_type = (body.get("task_type") or "t2v").strip()
    if task_type not in _VALID_TASK_TYPES:
        raise BadRequestException(
            message=(
                f"Invalid task_type '{task_type}'. "
                f"Expected one of: {', '.join(sorted(_VALID_TASK_TYPES))}"
            ),
            is_openai_exception=True,
        )
    # user_id comes from new-api (its end-user), not GPUStack's auth user; 0 default.
    try:
        user_id = int(body.get("user_id", 0) or 0)
    except (TypeError, ValueError):
        user_id = 0
    return body, model_name, task_type, user_id


@router.post("/videos")
async def create_video_task(request: Request, user: CurrentUserDep):
    """Submit an async generation job (video or async image).

    Thin facade (see docs/lightx2v-backend-design.md §6.0): resolve model →
    persist input bytes to NFS → pick the least-pending RUNNING instance →
    record the task row → submit to the engine → record the affinity mapping →
    return a public ``task_id`` the client polls via GET /v1/videos/{id}. No
    parameter validation here — new-api validates upstream.
    """
    body, model_name, task_type, user_id = await _parse_video_request(request)

    public_id = uuid.uuid4().hex
    ext = _output_ext(task_type)
    out_root = _output_root()
    out_abs = os.path.join(
        out_root, _rel_path(task_type, model_name, user_id, public_id, ext)
    )

    async with async_session() as session:
        model = await _resolve_target_model(session, request, user, model_name)
        running = await ModelInstanceService(session).get_running_instances(model.id)
        instance = await select_least_pending_instance(session, running)
        if instance is None:
            raise ServiceUnavailableException(
                message="No running instances available",
                is_openai_exception=True,
            )
        worker: Worker = await WorkerService(session).get_by_id(instance.worker_id)
        if not worker:
            raise InternalServerErrorException(
                message=f"Worker {instance.worker_id} not found",
                is_openai_exception=True,
            )

    # Build the engine body: pass request fields through untouched (no
    # validation), override the input fields with resolved NFS/URL locations,
    # and dictate the output path so the result lands at our §7.2 NFS path.
    input_overrides = await _persist_input(
        body, task_type, model_name, user_id, public_id
    )
    engine_body: Dict[str, Any] = {
        k: v
        for k, v in body.items()
        if k not in _CONTROL_KEYS and k not in _ENGINE_OWNED_FIELDS
    }
    engine_body.update(input_overrides)
    engine_body["save_result_path"] = out_abs
    await asyncio.to_thread(_ensure_parent_dir, out_abs)

    # Persist BEFORE the engine accepts work: if the row were written after a
    # successful submit and the insert failed, the engine would keep generating
    # an orphaned job that no sweeper or least-pending count could ever see,
    # and the client's retry would duplicate it. The row is created ASSIGNED
    # (not QUEUED — the sweeper redispatches QUEUED rows and could double-submit
    # during our up-to-30s submit window) with native_task_id=None; the sweeper
    # requeues stale ASSIGNED rows without a native id, covering a server crash
    # between this insert and the update below.
    async with async_session() as session:
        await VideoGenerationTask.create(
            session,
            VideoGenerationTask(
                task_id=public_id,
                model_id=model.id,
                model_name=model_name,
                user_id=user_id,
                owner_user_id=user.id,
                task_type=task_type,
                prompt=body.get("prompt"),
                params=engine_body,
                state=VideoTaskStateEnum.ASSIGNED,
                instance_id=instance.id,
                nfs_path=out_abs,
                output_root=out_root,
            ),
        )

    kind = _engine_kind(task_type)
    native_task_id, err = await _submit_to_engine(
        request.app.state.http_client,
        request.app.state.http_client_no_proxy,
        worker,
        instance,
        kind,
        engine_body,
    )

    async with async_session() as session:
        task = await VideoGenerationTask.one_by_field(session, "task_id", public_id)
        if err is not None:
            # Submit failed — the engine holds nothing, so drop the row and
            # surface the failure. Preserve the engine/worker failure class:
            # 5xx (503 backpressure, 500/502/504 transient backend faults, or
            # our synthetic 502 for a malformed engine response) must surface
            # as 5xx so new-api retries rather than treating it as a
            # non-retryable bad request. Only genuine 4xx (e.g. the engine's
            # 413 for oversized params) collapse to 400.
            if task:
                await task.delete(session)
            if err.status >= 500:
                raise ServiceUnavailableException(
                    message=err.message, is_openai_exception=True
                )
            raise BadRequestException(message=err.message, is_openai_exception=True)
        await task.update(session, {"native_task_id": native_task_id})
    logger.info(
        f"Video task {public_id} assigned to instance {instance.id} "
        f"(native={native_task_id}, model={model_name})"
    )
    return _public(task)


class _SubmitError(NamedTuple):
    status: int
    message: str
    # "busy": engine backpressure — retrying later is expected, costs nothing.
    # "permanent": the engine rejected the request itself — no retry can help.
    # "transient": unreachable instance / 5xx / malformed response.
    kind: str


async def _submit_to_engine(
    proxy_client,
    no_proxy_client,
    worker: Worker,
    instance: ModelInstance,
    kind: str,
    engine_body: Dict[str, Any],
) -> Tuple[Optional[str], Optional[_SubmitError]]:
    """Returns (native_task_id, None) on success, or (None, _SubmitError)."""
    try:
        resp, body_bytes = await request_to_worker(
            worker=worker,
            method="POST",
            path=f"v1/tasks/{kind}/",
            proxy_client=proxy_client,
            no_proxy_client=no_proxy_client,
            data=json.dumps(engine_body).encode(),
            headers=_instance_headers(instance),
            timeout=aiohttp.ClientTimeout(total=_SUBMIT_TIMEOUT),
            raise_on_error=False,
        )
    except Exception as e:
        logger.warning(f"Video submit to instance {instance.id} failed: {e}")
        return None, _SubmitError(503, f"Failed to reach instance: {e}", "transient")

    if resp.status == 503:
        # Engine per-instance FIFO is full — surface as backpressure so new-api
        # self-throttles (§6.0). least-pending already steered away from busy
        # instances; this is the residual overflow.
        return None, _SubmitError(
            503, "All instances busy, please retry shortly", "busy"
        )
    if resp.status >= 400:
        detail = body_bytes.decode(errors="replace") if body_bytes else ""
        error_kind = "transient" if resp.status >= 500 else "permanent"
        return None, _SubmitError(
            resp.status, f"Engine rejected task: {detail}", error_kind
        )

    try:
        native_task_id = json.loads(body_bytes).get("task_id")
    except Exception as e:
        return None, _SubmitError(502, f"Malformed engine response: {e}", "transient")
    if not native_task_id:
        return None, _SubmitError(502, "Engine did not return a task id", "transient")
    return native_task_id, None


def _retry_output_path(path: str, attempt: int) -> str:
    """A fresh save_result_path per dispatch attempt. If a presumed-dead
    instance was actually alive (transient state flap), its old run keeps
    writing the OLD path and can never corrupt this attempt's result."""
    base, ext = os.path.splitext(path)
    base = re.sub(r"-r\d+$", "", base)
    return f"{base}-r{attempt}{ext}"


async def _fail_task(session: AsyncSession, task, message: str, error_type: str):
    await task.update(
        session,
        {
            "state": VideoTaskStateEnum.FAILED,
            "state_message": message,
            "error_type": error_type,
        },
    )
    logger.warning(f"Video task {task.task_id} failed: {message}")


async def redispatch_task(
    session: AsyncSession,
    proxy_client,
    no_proxy_client,
    task: VideoGenerationTask,
) -> bool:
    """Re-dispatch a QUEUED task to the least-pending RUNNING instance, reusing
    the original engine body (``params`` — NFS input paths are still valid; the
    output path is regenerated per attempt). Used by the death-requeue sweeper.
    Returns True on success.

    Failure handling: no available instance / engine backpressure leaves the
    task QUEUED at no retry cost (waiting for capacity is normal); a permanent
    engine rejection (4xx) fails the task immediately; transient faults consume
    one retry and leave it QUEUED, up to _MAX_DISPATCH_RETRIES."""
    if not task.model_id:
        await _fail_task(session, task, "task has no model", "dispatch_failed")
        return False
    attempts = task.retry_count or 0
    if attempts >= _MAX_DISPATCH_RETRIES:
        await _fail_task(
            session,
            task,
            f"dispatch retry cap ({_MAX_DISPATCH_RETRIES}) exceeded",
            "retry_exhausted",
        )
        return False

    running = await ModelInstanceService(session).get_running_instances(task.model_id)
    instance = await select_least_pending_instance(session, running)
    if instance is None:
        return False
    worker: Worker = await WorkerService(session).get_by_id(instance.worker_id)
    if not worker:
        return False

    engine_body = dict(task.params or {})
    old_path = engine_body.get("save_result_path") or task.nfs_path
    if not old_path:
        await _fail_task(session, task, "task has no output path", "dispatch_failed")
        return False
    out_path = _retry_output_path(old_path, attempts + 1)
    engine_body["save_result_path"] = out_path
    await asyncio.to_thread(_ensure_parent_dir, out_path)

    kind = _engine_kind(task.task_type)
    native_task_id, err = await _submit_to_engine(
        proxy_client, no_proxy_client, worker, instance, kind, engine_body
    )
    if err is not None:
        if err.kind == "permanent":
            await _fail_task(
                session,
                task,
                f"engine rejected re-dispatch: {err.message}",
                "dispatch_rejected",
            )
            return False
        if err.kind == "transient":
            await task.update(session, {"retry_count": attempts + 1})
        logger.debug(f"Re-dispatch of task {task.task_id} deferred: {err}")
        return False
    await task.update(
        session,
        {
            "state": VideoTaskStateEnum.ASSIGNED,
            "instance_id": instance.id,
            "native_task_id": native_task_id,
            "state_message": None,
            "nfs_path": out_path,
            "params": engine_body,
            "retry_count": attempts + 1,
        },
    )
    logger.info(
        f"Re-dispatched task {task.task_id} to instance {instance.id} "
        f"(native={native_task_id}, output={out_path})"
    )
    return True


@router.get("/videos/{task_id}")
async def get_video_task(task_id: str, request: Request, user: CurrentUserDep):
    """Poll-on-GET: when the job is non-terminal, ask its mapped instance for the
    live engine status and fold it into the row (new-api's 15s poll drives
    progress; the sweeper's stale-task reconciliation is the fallback, §6.0)."""
    async with async_session() as session:
        task = await VideoGenerationTask.one_by_field(session, "task_id", task_id)
        if not task:
            raise NotFoundException(message="Task not found", is_openai_exception=True)
        _authorize_task(task, user)
        if task.state in VIDEO_TASK_TERMINAL_STATES:
            return _public(task)
        if not task.instance_id or not task.native_task_id:
            # Requeued and not yet re-dispatched by the sweeper.
            return _public(task)

        instance = await ModelInstanceService(session).get_by_id(task.instance_id)
        if not instance or instance.state != ModelInstanceStateEnum.RUNNING:
            # Instance is gone; leave the row for the death-requeue sweeper.
            return _public(task)
        worker: Worker = await WorkerService(session).get_by_id(instance.worker_id)
        if not worker:
            return _public(task)

    # Session closed — the worker round-trip (up to _STATUS_TIMEOUT) must not
    # pin a pooled DB connection, same convention as openai.py's proxy path.
    updates = await fetch_engine_status_updates(
        request.app.state.http_client,
        request.app.state.http_client_no_proxy,
        worker,
        instance,
        task,
    )
    if updates:
        async with async_session() as session:
            fresh = await VideoGenerationTask.one_by_field(session, "task_id", task_id)
            # Re-check under the new session: a concurrent poll/sweep may have
            # already moved the task; never downgrade a terminal state.
            if fresh and fresh.state not in VIDEO_TASK_TERMINAL_STATES:
                await fresh.update(session, updates)
                task = fresh
    return _public(task)


async def fetch_engine_status_updates(
    proxy_client,
    no_proxy_client,
    worker: Worker,
    instance: ModelInstance,
    task: VideoGenerationTask,
) -> Optional[Dict[str, Any]]:
    """Ask the task's mapped instance for the live engine status and translate
    it into a row-update dict (or None when there is nothing to fold). Holds no
    DB session — used by both the GET poll path and the sweeper's stale-task
    reconciliation."""
    try:
        resp, body_bytes = await request_to_worker(
            worker=worker,
            method="GET",
            path=f"v1/tasks/{task.native_task_id}/status",
            proxy_client=proxy_client,
            no_proxy_client=no_proxy_client,
            headers={router_header_key: f"{model_instance_prefix(instance)}.static"},
            timeout=aiohttp.ClientTimeout(total=_STATUS_TIMEOUT),
            raise_on_error=False,
        )
    except Exception as e:
        logger.debug(f"Status poll for task {task.task_id} failed: {e}")
        return None

    if resp.status == 404:
        # The instance is up but no longer knows this native id (restarted/evicted
        # its in-memory queue). Treat as a lost task the sweeper should requeue.
        return {
            "state": VideoTaskStateEnum.QUEUED,
            "instance_id": None,
            "native_task_id": None,
            "state_message": "instance lost task; requeued",
        }
    if resp.status >= 400 or not body_bytes:
        return None

    try:
        status = json.loads(body_bytes)
    except Exception:
        return None

    new_state = _ENGINE_STATE_MAP.get(status.get("status"))
    if new_state is None:
        return None
    updates: Dict[str, Any] = {"state": new_state}
    if new_state == VideoTaskStateEnum.FAILED:
        updates["state_message"] = status.get("error")
        updates["error_type"] = status.get("error_type") or None
    elif status.get("status") == "processing":
        updates["state_message"] = None
    return updates


@router.get("/videos/{task_id}/content")
async def get_video_task_content(task_id: str, user: CurrentUserDep):
    """Stream the result file straight from the shared NFS output (§7.4)."""
    async with async_session() as session:
        task = await VideoGenerationTask.one_by_field(session, "task_id", task_id)
    if not task:
        raise NotFoundException(message="Task not found", is_openai_exception=True)
    _authorize_task(task, user)
    if task.state != VideoTaskStateEnum.DONE or not task.nfs_path:
        raise NotFoundException(
            message="Task result not ready", is_openai_exception=True
        )
    # Defensive: the path is server-generated, but confirm it stays under the
    # output root and exists before streaming. Validate against the root that
    # was in effect when the task was created (recorded on the row) so editing
    # lightx2v_output_root at runtime doesn't 400 previously generated results.
    root = task.output_root or _output_root()
    resolved = os.path.realpath(task.nfs_path)
    if not resolved.startswith(os.path.realpath(root) + os.sep):
        raise BadRequestException(message="Invalid result path")
    if not os.path.isfile(resolved):
        raise NotFoundException(
            message="Result file missing on storage", is_openai_exception=True
        )
    return FileResponse(resolved, filename=os.path.basename(resolved))


def _public(task: VideoGenerationTask) -> Dict[str, Any]:
    """External contract: a stable public id, the lifecycle state, and the
    internal ``nfs_path`` (new-api reads it directly off the shared mount)."""
    return {
        "task_id": task.task_id,
        "status": task.state.value if task.state else None,
        "model": task.model_name,
        "task_type": task.task_type,
        "nfs_path": task.nfs_path if task.state == VideoTaskStateEnum.DONE else None,
        "error": task.state_message,
        "error_type": task.error_type,
    }
