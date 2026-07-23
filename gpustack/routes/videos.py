import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.api.exceptions import (
    BadRequestException,
    InternalServerErrorException,
    NotFoundException,
    ServiceUnavailableException,
    TooManyRequestsException,
)
from gpustack.config.config import get_global_config
from gpustack.gateway.utils import model_instance_prefix, router_header_key
from gpustack.http_proxy.strategies import select_least_pending_instance
from gpustack.schemas.models import BackendEnum, ModelInstance, ModelInstanceStateEnum
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
# /v1/tasks/video/. Everything else is a video task:
#   t2v/i2v/flf2v — Wan2.2 generation
#   s2v           — InfiniteTalk digital human (image + driving audio)
#   sr            — SeedVR2 video super-resolution (video in, sr_ratio out-scale)
#   vace          — Wan2.2 VACE video editing (src_video/src_mask/src_ref_images)
_IMAGE_TASK_TYPES = {"t2i", "i2i"}
_VIDEO_TASK_TYPES = {"t2v", "i2v", "flf2v", "s2v", "sr", "vace"}
# Audio (TTS) task types served by the IndexTTS-2 built-in engine. "tts" is the
# facade task_type; it maps to the engine's POST /v1/tasks/audio/ (engine kind
# "audio"). Zero-shot voice clone + emotion control, async like video.
_AUDIO_TASK_TYPES = {"tts"}
# Music task types served by the ACE-Step-1.5 built-in engine. They map to the
# engine's POST /v1/tasks/music/ (engine kind "music"): t2m (text-to-music, no
# input), cover (style transfer from a reference audio) and repaint (region
# regeneration). Async like video/audio.
_MUSIC_TASK_TYPES = {"t2m", "cover", "repaint"}
# Diffusion audio-generation task types served by the vLLM-Omni built-in engine's
# diffusion models. They map to the engine's POST /v1/tasks/audiogen/ (engine kind
# "audiogen"): AudioX (t2a sound-effects, v2a/v2m/tv2a/tv2m video->audio/music) and
# SoulX-Singer (svs singing voice synthesis). Distinct from TTS ("audio" kind) and
# ACE-Step music ("music" kind); NOTE "t2m" is NOT here — it belongs to ACE-Step
# (text-to-music), so AudioX text->music is not exposed via the facade (use v2m for
# video->music or ACE-Step for text->music). SVC is a later batch. Async like the
# rest; status/cancel reuse the global /v1/tasks/{id} endpoints.
_AUDIOGEN_TASK_TYPES = {"t2a", "v2a", "v2m", "tv2a", "tv2m", "svs"}
# Video-EDITING task types served by the Bernini built-in engine (native
# Bernini-R renderer). These are Bernini-EXCLUSIVE (no LightX2V collision) and
# map to engine kind "video" via the _engine_kind default (-> POST
# /v1/tasks/video/, .mp4, video latency): v2v (edit a source video by prompt),
# rv2v (source video + reference images), r2v (reference images -> video). Unlike
# LightX2V (which infers mode from input fields), Bernini's server picks its
# guidance_mode from task_type, so task_type is backfilled into the engine body
# below. v2v/rv2v/r2v are Bernini-exclusive. Bernini ALSO serves t2i/i2i/t2v,
# whose names are shared with LightX2V/image engines; since routing is by model
# (not task_type), these are disambiguated by the resolved model's backend at
# backfill time (see _BERNINI_SHARED_TASK_TYPES).
_BERNINI_TASK_TYPES = {"v2v", "rv2v", "r2v"}
# Shared-name generation modes Bernini also serves; task_type is backfilled for
# these ONLY when the resolved model's backend is Bernini.
_BERNINI_SHARED_TASK_TYPES = {"t2i", "i2i", "t2v"}
# Finite set of accepted actions. task_type is the first path component of the
# §7.2 NFS layout, so it MUST be constrained — an unsanitized value like
# "../../tmp/x" would let save_result_path / input writes escape the output root.
_VALID_TASK_TYPES = (
    _IMAGE_TASK_TYPES
    | _VIDEO_TASK_TYPES
    | _AUDIO_TASK_TYPES
    | _MUSIC_TASK_TYPES
    | _AUDIOGEN_TASK_TYPES
    | _BERNINI_TASK_TYPES
)

# Facade input field -> (engine request field, default extension). Inputs are
# NOT sent as base64/URL anymore: new-api (the trusted caller) pre-materializes
# every input onto the shared NFS and passes a relative path under <root>/inputs
# in the request's "input_refs"; the facade validates and maps it to the engine
# path field here (see docs/lightx2v-nfs-input-design.md §4). The extension is
# retained only for documentation of the expected content per field.
_INPUT_FIELDS = {
    "image": ("image_path", ".png"),
    "last_frame": ("last_frame_path", ".png"),
    "image_mask": ("image_mask_path", ".png"),
    "audio": ("audio_path", ".wav"),
    # TTS (IndexTTS-2, task_type "tts"): the zero-shot reference voice and an
    # optional emotion reference clip. Kept distinct from "audio" (video s2v's
    # driving audio) so a TTS voice maps to the engine's spk_audio_path, not
    # audio_path. Both are single NFS refs materialized by new-api.
    "voice": ("spk_audio_path", ".wav"),
    "emotion_audio": ("emo_audio_path", ".wav"),
    # SeedVR2 super-resolution (task_type "sr"): the low-res source video.
    "video": ("video_path", ".mp4"),
    # VACE video editing (task_type "vace"). The engine fields carry no _path
    # suffix — VideoTaskRequest names them src_video/src_mask/src_ref_images
    # verbatim (LightX2V server schema; empty strings are normalized to None
    # engine-side). src_mask is a mask VIDEO (white=repaint, gray=fill);
    # src_ref_images is a comma-joined image list (R2V reference mode).
    "src_video": ("src_video", ".mp4"),
    "src_mask": ("src_mask", ".mp4"),
    "src_ref_images": ("src_ref_images", ".png"),
    # Music (ACE-Step, task_type "cover"/"repaint"): the reference/source audio
    # whose style or region drives generation. t2m needs no input.
    "reference_audio": ("reference_audio_path", ".mp3"),
    "src_audio": ("src_audio_path", ".mp3"),
    # TTS voice-clone / dialogue (vLLM-Omni, task_type "tts"): the zero-shot
    # reference voice (VoxCPM2/CosyVoice3/GLM-TTS/MOSS-*) and, for MOSS-TTSD
    # multi-speaker dialogue, the second speaker's reference. Distinct from
    # IndexTTS "voice"->spk_audio_path because vLLM-Omni's request field for the
    # clone reference is ref_audio (a file). vLLM-Omni's PRESET voice (Qwen3-TTS
    # "vivian"/"ryan"/…) is a string, not a file: it does NOT use "voice" here
    # (that key is IndexTTS's file voice), but rides the engine's "voice" alias
    # "speaker" (AliasChoices("voice","speaker") in protocol/audio.py) — a caller
    # sends the preset name as the scalar "speaker", which is not an _INPUT_FIELDS
    # key and passes through the facade untouched to the engine. So both preset
    # voices (speaker) and cloning (ref_audio) work today; no IndexTTS-retire
    # dependency. ambient_sound / instructions / language are text scalars too.
    "ref_audio": ("ref_audio_path", ".wav"),
    "ref_audio_2": ("ref_audio_2_path", ".wav"),
    # Diffusion audio (vLLM-Omni, "audiogen" kind). AudioX video->audio/music
    # reuses the existing "video" field above (-> video_path; AudioX loads it via
    # av.open, a bare path, no file:// needed). SoulX-Singer SVS integrated
    # preprocess takes a prompt vocal (target timbre) + target accompaniment; the
    # engine field names match the SoulX extra_args keys (prompt_audio /
    # target_audio), which AudioGenTaskRequest.to_chat_request nests under
    # extra_args. Bare server paths (no file://).
    "prompt_audio": ("prompt_audio", ".wav"),
    "target_audio": ("target_audio", ".wav"),
}

# Facade fields that may carry a LIST of refs; each item is persisted and the
# engine gets a comma-separated path field (LightX2V splits on "," and reads
# each): "image" (multi-image edit, e.g. qwen-image-edit i2i) and
# "src_ref_images" (VACE R2V references). Other fields stay single. Cap the
# count as a facade backstop — the engine has no hard limit but many images
# blow VRAM.
_MULTI_INPUT_FIELDS = {"image", "src_ref_images"}
_MAX_INPUT_IMAGES = 5

# Max bytes for a single streamed input upload (POST /v1/videos/inputs). The UI
# streams browser file bytes here rather than inlining base64 (which inflates by
# ~33% and would balloon the JSON submit body — a source video for sr/vace is
# tens of MB). Enforced while streaming so an oversized file is cut off, not
# buffered whole. The production path (new-api) pre-materializes and never uses
# this endpoint.
_MAX_UPLOAD_BYTES = 256 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024
# Total multipart body ceiling for POST /v1/videos/inputs, checked against
# Content-Length BEFORE the body is read so an oversized upload is refused
# without spooling to the API server's temp disk. Sized to roughly one
# _MAX_UPLOAD_BYTES file plus framing: this makes the per-file cap effective at
# ingress for single-file fields, and multi-file fields (src_ref_images) are
# reference IMAGES that comfortably fit — a request can never spool more than
# ~one max file to temp regardless of field.
_UPLOAD_MAX_BODY = _MAX_UPLOAD_BYTES + 4 * 1024 * 1024

# Control keys consumed by the facade; never forwarded verbatim to the engine.
# "input_refs" carries the pre-materialized NFS input paths — either written by
# new-api (production) or by this server's POST /v1/videos/inputs upload endpoint
# (the gpustack-ui admin path). The raw _INPUT_FIELDS keys are rejected outright
# (see _parse_video_request) but kept here so a stray one can't leak downstream.
_CONTROL_KEYS = {"model", "task_type", "user_id", "input_refs"} | set(
    _INPUT_FIELDS.keys()
)

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
    "spk_audio_path",
    "emo_audio_path",
    "video_path",
    "src_video",
    "src_mask",
    "src_ref_images",
    # Music (ACE-Step) engine-owned path fields: reject if sent raw in the body
    # (they must arrive as validated NFS input_refs, not caller-supplied paths).
    "reference_audio_path",
    "src_audio_path",
    # TTS voice-clone / dialogue (vLLM-Omni) engine-owned path fields.
    "ref_audio_path",
    "ref_audio_2_path",
    # Diffusion audio (vLLM-Omni "audiogen") engine-owned path fields. video_path
    # is already listed above (shared with SeedVR2 sr).
    "prompt_audio",
    "target_audio",
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

# Admission-control fallback latency (seconds) when a model isn't in the config's
# lightx2v_model_latency_seconds table — per engine kind (image is fast, video
# slow). Deliberately conservative so an unknown model doesn't over-admit.
_DEFAULT_IMAGE_LATENCY = 20
_DEFAULT_VIDEO_LATENCY = 90
# IndexTTS-2 at RTF~3 does a short line (5-8s audio) in a handful of seconds;
# 20s is a conservative per-instance fallback that also absorbs longer lines.
_DEFAULT_AUDIO_LATENCY = 20
# ACE-Step turbo/xl-turbo generate a 30s clip in ~10s warm; longer clips scale
# but stay well under a minute. 30s is a conservative per-instance fallback.
_DEFAULT_MUSIC_LATENCY = 30
# Diffusion audio (AudioX ~10-30s for 250 steps / 10s clip; SoulX ~10-50s per
# song). 30s is a conservative per-instance fallback that absorbs both.
_DEFAULT_AUDIOGEN_LATENCY = 30


def _engine_kind(task_type: str) -> str:
    if task_type in _IMAGE_TASK_TYPES:
        return "image"
    if task_type in _AUDIO_TASK_TYPES:
        return "audio"
    if task_type in _MUSIC_TASK_TYPES:
        return "music"
    if task_type in _AUDIOGEN_TASK_TYPES:
        return "audiogen"
    return "video"


def _output_ext(task_type: str) -> str:
    kind = _engine_kind(task_type)
    if kind == "image":
        return ".png"
    if kind == "audio":
        return ".wav"
    if kind == "music":
        return ".mp3"
    if kind == "audiogen":
        return ".wav"
    return ".mp4"


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


def _validate_input_ref(ref: Any, root: str, user_id: int, field: str) -> str:
    """Validate one caller-supplied relative input ref and return the absolute
    engine-visible path.

    new-api (the only holder of the facade key) has already written the file to
    the shared NFS under the §3 convention
    (inputs/<task_type>-<model>/YYYY/MM/DD/<user_id>/<gid>-<field>[-i].<ext>).
    The facade never trusts a raw absolute path (IDOR): it only accepts a path
    RELATIVE to <root> and re-derives the absolute path itself. Rejects (400):
    non-string/empty; absolute; anything that (after normpath/realpath) escapes
    <root>/inputs; a user_id segment != the request user (cross-tenant read); a
    missing file.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise BadRequestException(
            message=f"Invalid input ref for '{field}'", is_openai_exception=True
        )
    ref = ref.strip()
    if os.path.isabs(ref) or ref.startswith("/"):
        raise BadRequestException(
            message=f"Input ref for '{field}' must be relative to the NFS root",
            is_openai_exception=True,
        )
    norm = os.path.normpath(ref)
    # inputs/ subtree only: results live under <root>/<feature>/... (not inputs/),
    # so this also blocks using another task's OUTPUT as an input.
    if norm != "inputs" and not norm.startswith("inputs" + os.sep):
        raise BadRequestException(
            message=f"Input ref for '{field}' must be under inputs/",
            is_openai_exception=True,
        )
    abs_path = os.path.join(root, norm)
    resolved = os.path.realpath(abs_path)
    inputs_root = os.path.realpath(os.path.join(root, "inputs"))
    if not resolved.startswith(inputs_root + os.sep):
        raise BadRequestException(
            message=f"Input ref for '{field}' escapes the inputs root",
            is_openai_exception=True,
        )
    # Tenant binding: the file's immediate parent dir is the owning user_id (§3).
    # Check it on the REALPATH-resolved target, not the raw ref — otherwise a
    # symlink under inputs/<user>/ pointing into another tenant's dir would pass
    # (it stays under inputs_root and the raw segment reads as this user) yet the
    # engine would read the other tenant's file. Resolving first closes that.
    if Path(resolved).parent.name != str(user_id):
        raise BadRequestException(
            message=f"Input ref for '{field}' does not match the request user",
            is_openai_exception=True,
        )
    if not os.path.isfile(resolved):
        raise BadRequestException(
            message=f"Input file for '{field}' not found on storage",
            is_openai_exception=True,
        )
    return abs_path


def _as_ref_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v]
    return [value]


def _check_input_constraints(counts: Dict[str, int]) -> None:
    """Cross-field + per-field cardinality constraints on a submit's input_refs.
    ``counts`` maps a facade field to how many items it carries.

    A mask edits exactly one base image; a VACE src_mask masks a src_video;
    only _MULTI_INPUT_FIELDS may carry more than one item (up to
    _MAX_INPUT_IMAGES). Enforced BEFORE any expensive work so a bad combo fails
    fast instead of wasting queue/GPU.
    """
    if counts.get("image_mask", 0) and counts.get("image", 0) != 1:
        # Reject both "mask + many images" and "mask + no image" (the latter
        # would submit image_mask_path with no image_path).
        raise BadRequestException(
            message="image_mask requires exactly one image",
            is_openai_exception=True,
        )
    if counts.get("src_mask", 0) and not counts.get("src_video", 0):
        # VACE MV2V: the mask video selects regions OF the source video.
        raise BadRequestException(
            message="src_mask requires src_video",
            is_openai_exception=True,
        )
    for field, n in counts.items():
        if field in _MULTI_INPUT_FIELDS:
            if n > _MAX_INPUT_IMAGES:
                raise BadRequestException(
                    message=(
                        f"Too many '{field}' inputs: {n} (max {_MAX_INPUT_IMAGES})"
                    ),
                    is_openai_exception=True,
                )
        elif n > 1:
            raise BadRequestException(
                message=f"'{field}' accepts a single input, got {n}",
                is_openai_exception=True,
            )


def _resolve_input_refs(input_refs: Any, user_id: int) -> Dict[str, str]:
    """Validate the caller's pre-materialized NFS input refs and map them to
    engine path fields → {engine_field: comma-joined absolute path(s)}.

    input_refs shape: {"image": [rel, ...], "last_frame": [rel], ...} with keys
    in _INPUT_FIELDS. Used by the new-api path, which pre-materializes every
    input onto shared NFS.
    """
    if input_refs is None:
        return {}
    if not isinstance(input_refs, dict):
        raise BadRequestException(
            message="input_refs must be an object", is_openai_exception=True
        )
    root = _output_root()
    per_field = {f: _as_ref_list(input_refs.get(f)) for f in _INPUT_FIELDS}
    _check_input_constraints({f: len(v) for f, v in per_field.items()})
    overrides: Dict[str, str] = {}
    for field, (engine_field, _ext) in _INPUT_FIELDS.items():
        refs = per_field[field]
        if not refs:
            continue
        overrides[engine_field] = ",".join(
            _validate_input_ref(r, root, user_id, field) for r in refs
        )
    return overrides


def _model_latency(cfg, model_name: str, task_type: str) -> int:
    """Per-model single-instance latency (seconds) for the admission estimate.
    Config table keyed by model name (case-insensitive substring match); falls
    back to a per-kind default for unknown models."""
    table = getattr(cfg, "lightx2v_model_latency_seconds", None) or {}
    name = (model_name or "").lower()
    for key, val in table.items():
        if key and str(key).lower() in name:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    kind = _engine_kind(task_type)
    if kind == "image":
        return _DEFAULT_IMAGE_LATENCY
    if kind == "audio":
        return _DEFAULT_AUDIO_LATENCY
    if kind == "music":
        return _DEFAULT_MUSIC_LATENCY
    if kind == "audiogen":
        return _DEFAULT_AUDIOGEN_LATENCY
    return _DEFAULT_VIDEO_LATENCY


async def _check_admission(
    session: AsyncSession,
    model_id,
    model_name: str,
    task_type: str,
    running: list,
) -> None:
    """Admission control (backpressure): reject with 429 when the estimated queue
    wait for this model exceeds the configured tolerance, so a saturated cluster
    fails fast instead of letting the task sit QUEUED until an upstream sync poll
    times out. estimated_wait = floor(non_terminal / instances) * latency."""
    cfg = get_global_config()
    if not cfg or not getattr(cfg, "lightx2v_admission_enabled", True):
        return
    instances = len(running)
    if instances <= 0:
        # No capacity to reason about; the no-running-instance 503 handles it.
        return
    stmt = (
        select(func.count())
        .select_from(VideoGenerationTask)
        .where(
            VideoGenerationTask.model_id == model_id,
            VideoGenerationTask.state.notin_(list(VIDEO_TASK_TERMINAL_STATES)),
        )
    )
    depth = (await session.exec(stmt)).first() or 0
    latency = _model_latency(cfg, model_name, task_type)
    est_wait = (int(depth) // instances) * latency
    kind = _engine_kind(task_type)
    if kind == "image":
        max_wait = getattr(cfg, "lightx2v_image_max_queue_wait_seconds", 25)
    elif kind == "audio":
        max_wait = getattr(cfg, "lightx2v_audio_max_queue_wait_seconds", 60)
    elif kind == "music":
        max_wait = getattr(cfg, "lightx2v_music_max_queue_wait_seconds", 90)
    elif kind == "audiogen":
        max_wait = getattr(cfg, "lightx2v_audiogen_max_queue_wait_seconds", 90)
    else:
        max_wait = getattr(cfg, "lightx2v_video_max_queue_wait_seconds", 150)
    if est_wait > int(max_wait):
        raise TooManyRequestsException(
            message=(
                "系统繁忙,请稍后再试 "
                f"(estimated queue wait {est_wait}s > {max_wait}s)"
            ),
            is_openai_exception=True,
        )


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
    # Raw base64/URL inputs are no longer accepted — inputs must be
    # pre-materialized onto shared NFS and passed via "input_refs" (§4.1). Fail
    # loud so a mis-integrated caller doesn't silently lose its images.
    raw_inputs = [f for f in _INPUT_FIELDS if body.get(f)]
    if raw_inputs:
        raise BadRequestException(
            message=(
                f"Raw inputs {raw_inputs} are no longer accepted; pass "
                "pre-materialized NFS paths via 'input_refs'"
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
        # Backpressure: reject fast (429) before creating any row if the queue
        # for this model is already too deep (§4.1).
        await _check_admission(session, model.id, model_name, task_type, running)
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
    # validation), map the validated NFS input refs to engine path fields, and
    # dictate the output path so the result lands at our §7.2 NFS path. Inputs
    # always arrive as pre-materialized relative paths in "input_refs" — written
    # by new-api (production) or by this server's /v1/videos/inputs upload
    # endpoint (gpustack-ui admin); the submit body itself never carries bytes.
    input_overrides = _resolve_input_refs(body.get("input_refs"), user_id)
    # Music cover/repaint need a driving audio; t2m is pure text. Fail fast before
    # queue/GPU (image i2v-style required-input checks live in the same spirit).
    if task_type == "cover" and "reference_audio_path" not in input_overrides:
        raise BadRequestException(
            message="reference_audio is required for cover",
            is_openai_exception=True,
        )
    if task_type == "repaint" and "src_audio_path" not in input_overrides:
        raise BadRequestException(
            message="src_audio is required for repaint",
            is_openai_exception=True,
        )
    engine_body: Dict[str, Any] = {
        k: v
        for k, v in body.items()
        if k not in _CONTROL_KEYS and k not in _ENGINE_OWNED_FIELDS
    }
    engine_body.update(input_overrides)
    engine_body["save_result_path"] = out_abs
    # Forward the audiogen subtype to the engine. task_type is a _CONTROL_KEY
    # (stripped above), but the diffusion engine needs to know WHICH AudioX mode
    # was requested — v2a vs v2m (or t2a) take the same inputs yet produce
    # different output (sound-effect vs music). Backfill audiox_task from task_type
    # for the AudioX modes so a direct caller that sent only task_type (not
    # audiox_task) still routes correctly; a caller-supplied audiox_task wins.
    # SoulX svs is single-mode (the loaded pipeline determines it), so it needs no
    # subtype field.
    if task_type in _AUDIOGEN_TASK_TYPES and task_type != "svs":
        engine_body.setdefault("audiox_task", task_type)
    # task_type is stripped as a control key, but Bernini's server selects its
    # guidance_mode from task_type (v2v/rv2v/r2v -> different guidance paths), so
    # backfill it into the engine body. v2v/rv2v/r2v are Bernini-exclusive (no
    # collision); t2i/i2i/t2v are shared names, backfilled ONLY when the resolved
    # model's backend is Bernini (routing is by model, so this is safe and won't
    # inject task_type into LightX2V/image engines). Caller-supplied task_type wins.
    if task_type in _BERNINI_TASK_TYPES or (
        model.backend == BackendEnum.BERNINI and task_type in _BERNINI_SHARED_TASK_TYPES
    ):
        engine_body.setdefault("task_type", task_type)
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


def _stream_upload_to_nfs(src, abs_path: str) -> int:
    """Copy an upload's spooled bytes to abs_path in _UPLOAD_CHUNK pieces,
    enforcing _MAX_UPLOAD_BYTES as it goes (never buffers the whole file).
    Runs in a worker thread — all blocking IO. Raises ValueError on overflow."""
    src.seek(0)
    written = 0
    with open(abs_path, "wb") as out:
        while True:
            chunk = src.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > _MAX_UPLOAD_BYTES:
                raise ValueError("upload too large")
            out.write(chunk)
    return written


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _validate_upload_field(task_type: str, model: str, field: str, files: list) -> bool:
    """Validate an upload request; return whether the field is multi-valued.
    Raises BadRequestException on any invalid input."""
    if task_type not in _VALID_TASK_TYPES:
        raise BadRequestException(
            message=f"Invalid task_type '{task_type}'", is_openai_exception=True
        )
    if field not in _INPUT_FIELDS:
        raise BadRequestException(
            message=(
                f"Invalid input field '{field}'. "
                f"Expected one of: {', '.join(sorted(_INPUT_FIELDS))}"
            ),
            is_openai_exception=True,
        )
    if not (model or "").strip():
        raise BadRequestException(
            message="Missing 'model' field", is_openai_exception=True
        )
    if not files:
        raise BadRequestException(
            message=f"No files for '{field}'", is_openai_exception=True
        )
    multi = field in _MULTI_INPUT_FIELDS
    if not multi and len(files) > 1:
        raise BadRequestException(
            message=f"'{field}' accepts a single file, got {len(files)}",
            is_openai_exception=True,
        )
    if multi and len(files) > _MAX_INPUT_IMAGES:
        raise BadRequestException(
            message=f"Too many '{field}' files: {len(files)} (max {_MAX_INPUT_IMAGES})",
            is_openai_exception=True,
        )
    return multi


async def _persist_upload_files(
    files: list, task_type: str, model: str, field: str, multi: bool, user_id: int
) -> Tuple[List[str], str]:
    """Stream each uploaded file to NFS under the §3 inputs convention, rolling
    back every write if any file fails (so a later-file error doesn't orphan
    earlier NFS writes — no task owns them). Returns (relative refs, group id)."""
    _, ext = _INPUT_FIELDS[field]
    root = _output_root()
    now = datetime.now(timezone.utc)
    gid = uuid.uuid4().hex
    base_dir = f"inputs/{task_type}-{_sanitize(model)}/{now:%Y/%m/%d}/{user_id}"
    rels: List[str] = []
    written_abs: List[str] = []
    try:
        for i, up in enumerate(files):
            name = f"{gid}-{field}-{i}{ext}" if multi else f"{gid}-{field}{ext}"
            rel = f"{base_dir}/{name}"
            abs_path = os.path.join(root, rel)
            await asyncio.to_thread(_ensure_parent_dir, abs_path)
            try:
                written = await asyncio.to_thread(
                    _stream_upload_to_nfs, up.file, abs_path
                )
            except ValueError:
                await asyncio.to_thread(_safe_remove, abs_path)
                raise BadRequestException(
                    message=(
                        f"Upload for '{field}' exceeds "
                        f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB"
                    ),
                    is_openai_exception=True,
                )
            if written == 0:
                await asyncio.to_thread(_safe_remove, abs_path)
                raise BadRequestException(
                    message=f"Upload for '{field}' is empty",
                    is_openai_exception=True,
                )
            written_abs.append(abs_path)
            rels.append(rel)
    except BaseException:
        for p in written_abs:
            await asyncio.to_thread(_safe_remove, p)
        raise
    return rels, gid


@router.post("/videos/inputs")
async def upload_video_input(request: Request, user: CurrentUserDep):
    """Materialize one input field's file(s) onto shared NFS and return their
    relative refs for a subsequent POST /v1/videos.

    This is the gpustack-ui admin path: that UI talks only to this server and has
    no new-api materialization layer, so it streams browser file bytes here (NOT
    inline base64 in the submit body — a source video is tens of MB and base64
    inflates it ~33%). The server — which mounts the same RW NFS — writes them
    under the §3 convention, mirroring what new-api does for production traffic;
    the returned input_refs then flow through the exact same _resolve_input_refs
    validation on submit. One field per call (the UI calls once per input);
    cross-field constraints (mask needs video, etc.) are enforced at submit.

    Returns {"input_refs": {field: [rel, ...]}, "user_id": <id>, "group_id": <id>}.
    The caller MUST submit /v1/videos with the SAME user_id so the ref's tenant
    segment matches (see _validate_input_ref).
    """
    # Bound request ingestion BEFORE Starlette spools the multipart to the API
    # server's temp disk (the per-file _stream_upload_to_nfs cap only bounds the
    # NFS copy, and Starlette's max_part_size does NOT limit FILE parts — only
    # text fields). The guarantee is Content-Length: require it (a browser
    # FormData upload always sends it) and cap it, so a chunked/omitted-length
    # client can't spool an unbounded part and an honest body can't exceed
    # _UPLOAD_MAX_BODY. The fronting gateway should also cap body size.
    declared = request.headers.get("content-length")
    if not declared or not declared.isdigit():
        raise BadRequestException(
            message="Content-Length is required for uploads",
            is_openai_exception=True,
        )
    if int(declared) > _UPLOAD_MAX_BODY:
        raise BadRequestException(
            message=(
                f"Upload too large (max ~{_UPLOAD_MAX_BODY // (1024 * 1024)} MiB "
                "per request)"
            ),
            is_openai_exception=True,
        )
    # max_part_size still caps the small text fields (task_type/model/field);
    # file parts are bounded by the Content-Length ceiling above.
    try:
        form = await request.form(max_part_size=_UPLOAD_CHUNK)
    except Exception as e:
        raise BadRequestException(
            message=f"Invalid multipart upload: {e}", is_openai_exception=True
        )
    # form.close() in the finally frees every spooled UploadFile on ANY exit —
    # success or a validation error — so a rejected large upload can't leave temp
    # files/FDs dangling until GC (Starlette only auto-closes when the form is
    # used as an async context manager).
    try:
        task_type = str(form.get("task_type") or "").strip()
        model = str(form.get("model") or "")
        field = str(form.get("field") or "")
        # File parts are UploadFile (have .file); text parts are str — keep files.
        files = [f for f in form.getlist("files") if hasattr(f, "file")]
        multi = _validate_upload_field(task_type, model, field, files)
        # Authorize the model BEFORE writing anything to NFS — otherwise any
        # authenticated caller could name an arbitrary/forbidden model and fill
        # shared storage with orphaned uploads (they're never tied to a task).
        # 404 (not 403) to avoid confirming a model the caller can't see exists.
        async with async_session() as session:
            allowed = await UserService(session).model_allowed_for_user(
                model_name=model,
                user_id=user.id,
                api_key=getattr(request.state, "api_key", None),
            )
        if not allowed:
            raise NotFoundException(message="Model not found", is_openai_exception=True)
        rels, gid = await _persist_upload_files(
            files, task_type, model, field, multi, user.id
        )
        return {"input_refs": {field: rels}, "user_id": user.id, "group_id": gid}
    finally:
        await form.close()


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


async def _cancel_on_engine(
    proxy_client,
    no_proxy_client,
    worker: Worker,
    instance: ModelInstance,
    native_task_id: str,
) -> None:
    """Best-effort: ask the mapped instance to abort a running/pending task
    (engine DELETE /v1/tasks/{id}). Failure is non-fatal — the row is already
    CANCELED (authoritative) and any orphan output is reaped by the janitor."""
    try:
        await request_to_worker(
            worker=worker,
            method="DELETE",
            path=f"v1/tasks/{native_task_id}",
            proxy_client=proxy_client,
            no_proxy_client=no_proxy_client,
            headers={router_header_key: f"{model_instance_prefix(instance)}.static"},
            timeout=aiohttp.ClientTimeout(total=_STATUS_TIMEOUT),
            raise_on_error=False,
        )
    except Exception as e:
        logger.debug(f"Engine cancel for native task {native_task_id} failed: {e}")


@router.post("/videos/{task_id}/cancel")
async def cancel_video_task(task_id: str, request: Request, user: CurrentUserDep):
    """Cancel a non-terminal task. Marks the row CANCELED (authoritative: the
    sweeper won't re-dispatch a terminal task, GET returns canceled, content
    404s) and best-effort tells the mapped instance to abort so the GPU stops.
    Idempotent on already-terminal tasks. Used by new-api on client disconnect /
    sync timeout to stop wasted generation (§4.2)."""
    async with async_session() as session:
        task = await VideoGenerationTask.one_by_field(session, "task_id", task_id)
        if not task:
            raise NotFoundException(message="Task not found", is_openai_exception=True)
        _authorize_task(task, user)
        if task.state in VIDEO_TASK_TERMINAL_STATES:
            return _public(task)
        instance = None
        worker = None
        native_task_id = task.native_task_id
        if task.instance_id and native_task_id:
            instance = await ModelInstanceService(session).get_by_id(task.instance_id)
            if instance and instance.state == ModelInstanceStateEnum.RUNNING:
                worker = await WorkerService(session).get_by_id(instance.worker_id)
        await task.update(
            session,
            {
                "state": VideoTaskStateEnum.CANCELED,
                "state_message": "canceled by client",
            },
        )
    # Engine abort outside the DB session (round-trip must not pin a connection).
    if worker and instance and native_task_id:
        await _cancel_on_engine(
            request.app.state.http_client,
            request.app.state.http_client_no_proxy,
            worker,
            instance,
            native_task_id,
        )
    async with async_session() as session:
        task = await VideoGenerationTask.one_by_field(session, "task_id", task_id)
    return _public(task)


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
