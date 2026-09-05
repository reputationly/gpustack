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
from sqlalchemy import and_, func, or_
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
from gpustack.schemas.models import (
    BackendEnum,
    ModelInstance,
    ModelInstanceStateEnum,
    is_video_model,
)
from gpustack.schemas.video_generation_task import (
    VIDEO_TASK_TERMINAL_STATES,
    VideoGenerationTask,
    VideoTaskStateEnum,
)
from gpustack.schemas.workers import Worker
from gpustack.server.db import async_session
from gpustack.server.deps import CurrentUserDep
from gpustack.server.video_progress import (
    ATTEMPT_RESET,
    elapsed_seconds,
    normalize_progress,
)
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
#   t2v/i2v/flf2v — Wan2.2 generation; ALSO MiniMax-H3 (vLLM-Omni), see _H3_TASK_MAP
#   l2va          — keyframe "last frame only" (MiniMax-H3 L2VA). Same input shape
#                   as i2v (exactly one image); only the SEMANTICS differ (that
#                   image is the LAST frame, not the first), so it cannot be
#                   inferred from the request shape and must be sent explicitly.
#   s2v           — InfiniteTalk digital human (image + driving audio)
#   sr            — SeedVR2 video super-resolution (video in, sr_ratio out-scale)
#   vace          — Wan2.2 VACE video editing (src_video/src_mask/src_ref_images)
#   v2a           — LTX-2.3 video dubbing (video in -> SAME pixels + AI audio
#                   track, .mp4 out). Reassigned 2026-07: v2a used to be an
#                   AudioX audiogen type (.wav out); that product is retired and
#                   the task CONTRACT is now "video in, dubbed video out". The
#                   task_type is model-agnostic — any future dubbing model that
#                   honors the contract deploys under it (routing is by model).
_IMAGE_TASK_TYPES = {"t2i", "i2i"}
_VIDEO_TASK_TYPES = {"t2v", "i2v", "l2va", "flf2v", "s2v", "r2va", "sr", "vace", "v2a"}

# MiniMax-H3 (vLLM-Omni backend) task selection.
#
# H3 picks its task from a NESTED key, extra_params.task — there is no top-level
# `task` field, and VideoGenerationRequest has no extra="forbid", so a top-level
# one is silently DROPPED (no error, no effect). Same trap as the IndexTTS-2
# emotion scalars.
#
# The facade's own task_type vocabulary is kept as-is (new-api's tab<->task_type
# reverse index, playground config and billing matrix are all built on it); the
# translation to H3's engine vocabulary happens here, exactly like the Bernini
# guidance_mode and AudioX audiox_task backfills below.
#
# frame_indices tells the single FL2VA checkpoint WHERE the supplied image(s)
# land on the timeline — this is what lets one checkpoint serve first-frame,
# last-frame and first+last from the same weights (Wan needed two separately
# launched instances for this). The engine accepts only these three values and
# requires len(frame_indices) == len(images).
#
# H3-EXCLUSIVE task types, mirroring the _BERNINI_TASK_TYPES /
# _BERNINI_SHARED_TASK_TYPES split.
#
# Only l2va is exclusive. t2v / i2v / flf2v / s2v are SHARED names that LightX2V
# (Wan2.2) and InfiniteTalk legitimately serve, so they must not be gated on the
# backend — routing is by model, and the backfill below already keys off it.
#
# l2va is different: it did not exist before H3, no other engine understands it,
# and its input shape (exactly one image) is indistinguishable from i2v. Admitting
# it on a LightX2V model would render a first-frame i2v with no error at all —
# see the guard in create_video_task.
_H3_ONLY_TASK_TYPES = {"l2va", "r2va"}

# Task types that need the Ref2VA checkpoint PARTITION, not just an H3 model.
#
# H3 ships two partitions as two separate weight sets, and one process loads
# exactly one of them (`pipeline_minimax_h3` raises
# "checkpoint partition 'fl2va' supports ['fl2va','t2va'], got task='ref2va'").
# Routing is by model, so an operator can point these at the FL2VA deployment and
# the request is only rejected after dispatch.
#
# That supported set is built at LOAD time from the checkpoint's own
# model_index.json (`pipeline_minimax_h3`, self.supported_tasks) — and a COMBINED
# build, i.e. one weights root that also carries a Ref2VA/ subdir, gets ref2va
# merged into it and serves both partitions from a single deployment. Nothing
# visible from the facade distinguishes that layout from an FL2VA-only one, which
# is why _h3_ref2va_capability reports None instead of guessing and the guard in
# create_video_task fails open on it.
_H3_REF2VA_TASK_TYPES = {"s2v", "r2va"}


def _h3_ref2va_capability(model) -> bool | None:
    """Can this model serve the ref2va partition? None = cannot tell.

    Judged on what the deployment ACTUALLY LOADED, not on the display name:
    the name is operator-chosen and arbitrary, whereas the weight path and
    --task-type are the two things that decide which partition comes up.

    Returns None (rather than False) whenever there is no positive evidence —
    combined mode loads BOTH partitions, and an unrecognised layout must not be
    rejected on a guess. Callers treat None as "allow, let the engine decide".
    """
    argv = " ".join(model.backend_parameters or []).lower()
    # --task-type is authoritative when present: it selects the partition.
    if "ref2va" in argv:
        return True
    if "fl2va" in argv:
        return False
    # Otherwise fall back to the weight path. Production does NOT pass
    # --task-type (the checkpoint's model_index.json declares the partition), so
    # this is the common case: .../MiniMax-H3/Ref2VA vs .../MiniMax-H3-FL2VA-INT8
    source = (model.model_source_key or "").lower()
    if "ref2va" in source:
        return True
    if "fl2va" in source:
        return False
    # Combined mode (whole MiniMax-H3 dir, both partitions) or anything we do not
    # recognise: no evidence either way.
    return None


# Tokens that positively identify a MiniMax-H3 deployment from the weight path or
# the launch argv. Same two sources _h3_ref2va_capability trusts, and for the same
# reason: the display name is operator-chosen and arbitrary, these two are not.
_H3_SOURCE_TOKENS = ("minimax-h3", "minimax_h3", "fl2va", "ref2va")


def _is_h3_video_deployment(model) -> bool:
    """Positive evidence that this vLLM-Omni deployment serves MiniMax-H3 video.

    vLLM-Omni is the one built-in backend that is NOT single-modality — the same
    backend runs the whole TTS fleet (VoxCPM2 / CosyVoice3 / Qwen3-TTS / MOSS-*)
    — so ``backend == VLLM_OMNI`` does not mean "this model makes video".

    Unlike _h3_ref2va_capability above, this one fails CLOSED, because the two
    guards have opposite failure economics. A ref2va/fl2va partition mismatch is
    caught by the engine and returned as a clean 4xx, so guessing wrong there
    only costs a round trip. A video task sent to a SPEECH deployment has no such
    backstop: POST /v1/tasks/video/ is registered on every vLLM-Omni server and
    app.state.openai_serving_video is assigned on the non-diffusion init path
    too, so the request is NOT refused at submit — it gets past handler
    resolution and dies deep in the job, and any 503 on the way out used to be
    rewritten as engine backpressure (see _submit_to_engine), i.e. a permanent
    misconfiguration that new-api would retry forever.
    """
    haystack = " ".join(
        [model.model_source_key or "", " ".join(model.backend_parameters or [])]
    ).lower()
    if any(token in haystack for token in _H3_SOURCE_TOKENS):
        return True
    # Escape hatch for a weights path that carries none of those tokens: an
    # explicitly declared video category also counts. set_model_categories()
    # returns early when categories are already populated, so an operator can
    # always force this open without renaming the checkpoint directory.
    return is_video_model(model)


#   facade task_type -> (extra_params.task, extra_params.frame_indices)
_H3_TASK_MAP = {
    "t2v": ("t2va", None),
    "i2v": ("fl2va", [0]),
    "l2va": ("fl2va", [-1]),
    "flf2v": ("fl2va", [0, -1]),
    # ref2va needs its own checkpoint partition and is NOT deployed yet (only a
    # BF16 144 GB build exists, no INT8). Mapped here so the rule is complete and
    # this block never has to be revisited when the digital-human line lands.
    "s2v": ("ref2va", None),
    # Mixed-reference Ref2VA: images + videos + audio, the engine's headline mode.
    # Same engine task as s2v; they differ only in which references the caller
    # may send (s2v is InfiniteTalk's one-image + one-driving-audio shape).
    "r2va": ("ref2va", None),
}
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
# "audiogen"): AudioX (t2a sound-effects, v2m/tv2m video->music) and SoulX-Singer
# (svs singing voice synthesis). Distinct from TTS ("audio" kind) and ACE-Step
# music ("music" kind); NOTE "t2m" is NOT here — it belongs to ACE-Step
# (text-to-music). SVC is a later batch. Async like the rest; status/cancel reuse
# the global /v1/tasks/{id} endpoints.
# RETIRED 2026-07: "v2a"/"tv2a" (AudioX video->audio, .wav out) — the video-
# dubbing product moved to LTX-2.3, and "v2a" now lives in _VIDEO_TASK_TYPES
# with a new contract (video in -> dubbed video out). "tv2a" is gone entirely
# (the new v2a takes an optional prompt, no separate with-text type). Drain any
# queued AudioX v2a tasks BEFORE deploying this change — the sweeper would
# redispatch them down the video route.
_AUDIOGEN_TASK_TYPES = {"t2a", "v2m", "tv2m", "svs"}
# Video-EDITING task types served by the Bernini built-in engine (native
# Bernini-R renderer). These are Bernini-EXCLUSIVE (no LightX2V collision) and
# map to engine kind "video" via the _engine_kind default (-> POST
# /v1/tasks/video/, .mp4, video latency): v2v (edit a source video by prompt),
# rv2v (source video + reference images), r2v (reference images -> video),
# mv2v (TWO source videos, multi-source edit), ads2v (TWO source videos, ad/screen
# insertion — same inputs as mv2v but a dedicated system prompt + rv2v-chain
# guidance in the engine). Unlike LightX2V (which infers mode from input fields),
# Bernini's server picks its guidance_mode + system prompt from task_type, so
# task_type is backfilled into the engine body below. These five are
# Bernini-exclusive. Bernini ALSO serves t2i/i2i/t2v, whose names are shared with
# LightX2V/image engines; since routing is by model (not task_type), these are
# disambiguated by the resolved model's backend at backfill time
# (see _BERNINI_SHARED_TASK_TYPES).
_BERNINI_TASK_TYPES = {"v2v", "rv2v", "r2v", "mv2v", "ads2v"}
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
# NOT sent as base64/URL anymore: new-api (the trusted caller) places every input
# on the shared NFS and passes a path relative to <root> in the request's
# "input_refs" — either a freshly materialized upload under inputs/, or a previous
# task's product referenced in place (§4.2); the facade validates and maps it to
# the engine path field here (see docs/lightx2v-nfs-input-design.md §4). The extension is
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
# "audio" and "video" are listed so MiniMax-H3 Ref2VA can carry several of each;
# _input_cap() clamps them back to 1 for every other task_type, so no existing
# single-file consumer is loosened.
_MULTI_INPUT_FIELDS = {"image", "src_ref_images", "src_video", "audio", "video"}
_MAX_INPUT_IMAGES = 5
# src_video may carry TWO videos ONLY for Bernini mv2v/ads2v (multi-source edit /
# ad insertion). Like src_ref_images, the facade comma-joins the refs into the
# single src_video string of the engine request; BERNINI'S API SERVER is the one
# that splits that comma-joined string back into the 2-element `video` list its
# pipeline takes (server.py _build_task_data) — the facade does NOT need to (and
# must not) reshape the field. Every OTHER src_video consumer (vace -> LightX2V,
# Bernini v2v/rv2v) contracts a SINGLE source video — a comma-joined pair would
# reach those engines as one invalid path, so the >1 allowance is task-type
# scoped.
_MAX_INPUT_VIDEOS = 2
_MULTI_VIDEO_TASK_TYPES = {"mv2v", "ads2v"}

# Per-task-type input caps that OVERRIDE the defaults above.
#
# MiniMax-H3 Ref2VA ("r2va") is the only task that takes genuinely MIXED
# references — up to 9 images + 3 videos + 3 standalone audio, 12 in total — and
# the engine enforces exactly those numbers
# (pipeline_minimax_h3._validate_ref2va_reference_counts). The playground only
# exposes the images+one-audio subset, but the public API must not be capped
# below what the engine can actually do.
#
# Note "audio" is multi ONLY here: every other consumer of the audio field
# (InfiniteTalk s2v driving audio, ACE-Step, TTS references) contracts exactly
# one file, and a comma-joined pair would reach those engines as one invalid
# path. Same reasoning as the src_video scoping.
_H3_REF2VA_TASK_TYPE = "r2va"
_TASK_INPUT_CAPS = {
    _H3_REF2VA_TASK_TYPE: {"image": 9, "video": 3, "audio": 3},
}
# Total reference cap across all modalities (engine: "at most 12 total").
_H3_REF2VA_MAX_TOTAL_REFS = 12


# Fields that are multi ONLY where a task-type override grants it. They live in
# _MULTI_INPUT_FIELDS so the plumbing can comma-join them, but their DEFAULT cap
# stays 1 — without this they would fall through to _MAX_INPUT_IMAGES and every
# single-file consumer (InfiniteTalk s2v driving audio, SeedVR2 source video,
# ACE-Step, TTS references) would silently start accepting five.
_TASK_SCOPED_MULTI_FIELDS = {"audio", "video"}


def _input_cap(field: str, task_type: str) -> int:
    """Max number of files allowed for a facade input field under this task."""
    override = _TASK_INPUT_CAPS.get(task_type, {}).get(field)
    if override is not None:
        return override
    if field in _TASK_SCOPED_MULTI_FIELDS:
        return 1
    if field == "src_video":
        return _MAX_INPUT_VIDEOS if task_type in _MULTI_VIDEO_TASK_TYPES else 1
    if field in _MULTI_INPUT_FIELDS:
        return _MAX_INPUT_IMAGES
    return 1


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
# _MAX_UPLOAD_BYTES file plus framing: this keeps the per-file cap effective at
# ingress for every field. mv2v/ads2v need TWO source videos, but the UI uploads
# video-kind files ONE PER REQUEST (each within this cap) and merges the returned
# refs — so the two-video allowance never requires a two-video-sized body, and a
# single oversized file can never spool past ~one cap to temp.
_UPLOAD_MAX_BODY = _MAX_UPLOAD_BYTES + 8 * 1024 * 1024

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
# NOT sufficient for every audio engine: Breeze TTS 2 runs at RTF 0.657 on the
# default tier, so its 600-char ceiling (111s of audio) takes ~71s of wall
# clock. A Breeze deployment must set a per-model override via
# lightx2v_model_latency_seconds, otherwise _check_admission's queue estimate
# is off by 3-4x on long scripts.
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

    new-api (the only holder of the facade key) has already placed the file on
    the shared NFS, in one of two layouts, both ending in .../<user_id>/<file>:

      - a freshly materialized upload, under the §3 convention
        inputs/<task_type>-<model>/YYYY/MM/DD/<user_id>/<gid>-<field>[-i].<ext>
      - a previous task's RESULT, <feature>-<model>/YYYY/MM/DD/<user_id>/<id>.<ext>,
        referenced in place (see below)

    The facade never trusts a raw absolute path (IDOR): it only accepts a path
    RELATIVE to <root> and re-derives the absolute path itself. Rejects (400):
    non-string/empty; absolute; anything that (after normpath/realpath) escapes
    <root>; a user_id segment != the request user (cross-tenant read); a missing
    file.
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
    # Storage-root subtree — deliberately wider than inputs/ alone. This used to
    # be "inputs/ only", whose stated purpose was to block using another task's
    # OUTPUT as an input; new-api now does exactly that on purpose: when the
    # caller passes back a product URL we issued (image-to-image, keyframe /
    # reference-to-video), the bytes are already on this same NFS, so it
    # references them in place instead of copying them into inputs/ first.
    # Rationale and the full argument for why this does not weaken isolation:
    # docs/lightx2v-nfs-input-design.md §4.2.
    #
    # Tenant isolation never rested on this prefix — it rests on the
    # parent-dir == user_id check below, which both layouts satisfy
    # (.../<user_id>/<file>). Containment is still enforced, just against the
    # storage root rather than <root>/inputs.
    #
    # Lifetime is safe: video_storage_janitor._protected_day_dirs derives the
    # protected day dirs from a task's params input-path fields via parent.parent,
    # which is layout-agnostic — a referenced product's day dir is protected for
    # as long as the referencing task is non-terminal.
    abs_path = os.path.join(root, norm)
    resolved = os.path.realpath(abs_path)
    root_real = os.path.realpath(root)
    if not resolved.startswith(root_real + os.sep):
        raise BadRequestException(
            message=f"Input ref for '{field}' escapes the storage root",
            is_openai_exception=True,
        )
    # Tenant binding: the file's immediate parent dir is the owning user_id (§3).
    # Check it on the REALPATH-resolved target, not the raw ref — otherwise a
    # symlink under <user>/ pointing into another tenant's dir would pass (it
    # stays under the storage root and the raw segment reads as this user) yet
    # the engine would read the other tenant's file. Resolving first closes that.
    # This is now the ONLY thing standing between tenants, since the prefix
    # restriction above was widened — do not weaken it.
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


def _check_input_constraints(counts: Dict[str, int], task_type: str = "") -> None:
    """Cross-field + per-field cardinality constraints on a submit's input_refs.
    ``counts`` maps a facade field to how many items it carries. ``task_type``
    scopes the src_video allowance: only mv2v/ads2v may carry two videos.

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
    # Bernini mv2v/ads2v are DEFINED as two-source-video modes (main + second
    # video). 0/1 videos would still create a task row and only fail inside the
    # engine; fail fast here instead (mirrors new-api's materializeBerniniInputs
    # and the gpustack-ui client check).
    if task_type in _MULTI_VIDEO_TASK_TYPES and counts.get("src_video", 0) != 2:
        raise BadRequestException(
            message=(
                f"task_type '{task_type}' requires exactly two src_video "
                f"inputs, got {counts.get('src_video', 0)}"
            ),
            is_openai_exception=True,
        )
    for field, n in counts.items():
        cap = _input_cap(field, task_type)
        if n > cap:
            raise BadRequestException(
                message=(
                    f"Too many '{field}' inputs: {n} (max {cap})"
                    if cap > 1
                    else f"'{field}' accepts a single input, got {n}"
                ),
                is_openai_exception=True,
            )
    # Engine-side total cap for mixed references; checked here so an over-large
    # combination fails before any NFS write or queue slot.
    if task_type == _H3_REF2VA_TASK_TYPE:
        total_refs = sum(counts.get(f, 0) for f in ("image", "video", "audio"))
        if total_refs > _H3_REF2VA_MAX_TOTAL_REFS:
            raise BadRequestException(
                message=(
                    f"task_type '{task_type}' accepts at most "
                    f"{_H3_REF2VA_MAX_TOTAL_REFS} references in total, "
                    f"got {total_refs}"
                ),
                is_openai_exception=True,
            )


def _resolve_input_refs(
    input_refs: Any, user_id: int, task_type: str = ""
) -> Dict[str, str]:
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
    _check_input_constraints({f: len(v) for f, v in per_field.items()}, task_type)
    overrides: Dict[str, str] = {}
    for field, (engine_field, _ext) in _INPUT_FIELDS.items():
        refs = per_field[field]
        if not refs:
            continue
        overrides[engine_field] = ",".join(
            _validate_input_ref(r, root, user_id, field) for r in refs
        )
    return overrides


def _lookup_by_model(table, model_name: str) -> Optional[int]:
    """Look up ``model_name`` in a per-model seconds table: **exact match first
    (case-insensitive), substring only as a fallback**. Shared by the latency and
    queue-wait tables so both use identical matching semantics. Returns None when
    nothing matches or every match is unparseable.

    Why two stages. Callers that already resolved the request's ``model`` string
    to a concrete Model pass ``model.name`` — the authoritative name — so the
    exact pass hits and row order is irrelevant. That kills the footgun the
    substring-only version had: a shorter key shadows a longer one
    (``qwen-image`` matching ``qwen-image-edit``), silently applying the wrong
    latency to whichever row happened to be listed first.

    The substring fallback stays because one caller cannot resolve: the progress
    poller reads ``task.model_name``, which is the **caller-supplied** string
    persisted at submit time. That may be a model-route name, an owner-prefixed
    ``owner/name``, or an upstream channel alias, none of which need equal any
    GPUStack model name. For those, substring is still the best guess available —
    and there row order does matter, longest key first."""
    name = (model_name or "").lower()
    if not name:
        return None
    items = [(str(key).lower(), val) for key, val in (table or {}).items() if key]
    # Two passes over the same rows, exact before substring.
    for matches in (lambda key: key == name, lambda key: key in name):
        for key, val in items:
            if not matches(key):
                continue
            try:
                return int(val)
            except (TypeError, ValueError):
                continue  # unparseable row: keep looking, don't fail the submit
    return None


def _model_latency(cfg, model_name: str, task_type: str) -> int:
    """Per-model single-instance latency (seconds) for the admission estimate.
    Config table keyed by model name (case-insensitive substring match); falls
    back to a per-kind default for unknown models."""
    override = _lookup_by_model(
        getattr(cfg, "lightx2v_model_latency_seconds", None), model_name
    )
    if override is not None:
        return override
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


def _model_queue_wait(cfg, model_name: str, task_type: str) -> int:
    """Tolerated queue wait (seconds) before admission rejects with 429.

    A per-model override (``lightx2v_model_queue_wait_seconds``) wins over the
    per-kind ceiling, so one unusually slow model does not force the whole kind's
    backpressure open. See the config field's comment for why: the image kind
    mixes ~8s (z-image) with ~110s (HunyuanImage-3.0) models, and a single shared
    ceiling cannot serve both.
    """
    override = _lookup_by_model(
        getattr(cfg, "lightx2v_model_queue_wait_seconds", None), model_name
    )
    if override is not None:
        return override
    kind = _engine_kind(task_type)
    if kind == "image":
        return getattr(cfg, "lightx2v_image_max_queue_wait_seconds", 25)
    if kind == "audio":
        return getattr(cfg, "lightx2v_audio_max_queue_wait_seconds", 60)
    if kind == "music":
        return getattr(cfg, "lightx2v_music_max_queue_wait_seconds", 90)
    if kind == "audiogen":
        return getattr(cfg, "lightx2v_audiogen_max_queue_wait_seconds", 90)
    return getattr(cfg, "lightx2v_video_max_queue_wait_seconds", 150)


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
    max_wait = _model_queue_wait(cfg, model_name, task_type)
    if est_wait > int(max_wait):
        raise TooManyRequestsException(
            message=(
                "系统繁忙,请稍后再试 "
                f"(estimated queue wait {est_wait}s > {max_wait}s)"
            ),
            is_openai_exception=True,
        )


def _backfill_h3_engine_params(
    engine_body: Dict[str, Any], backend, task_type: str
) -> None:
    """Translate our task_type into MiniMax-H3's nested extra_params.

    Gated on the resolved model's BACKEND, not on its name: routing is by model,
    and vLLM-Omni is the only backend that speaks this vocabulary. Same shape as
    the Bernini guidance_mode backfill.

    The backend test alone does not establish that the target is an H3 model —
    _engine_kind dispatches on task_type, never on the model, so a vLLM-Omni
    SPEECH deployment reaches this function whenever the caller names it with a
    video task_type. That is admission's job, not the backfill's: the H3-only
    types are rejected by _is_h3_video_deployment in create_video_task, and for
    the SHARED names (t2v / i2v / flf2v / s2v) writing extra_params.task onto a
    misrouted request changes nothing — the request was already going to the
    wrong engine, and the keys are inert to every engine but H3.

    A module-level function rather than an inline block for two reasons: it keeps
    create_video_task under the complexity cap (pre-commit allows 15 and the
    function was already at 11 before H3), and it lets the tests exercise the real
    code path instead of maintaining a copy of it.
    """
    if backend != BackendEnum.VLLM_OMNI or task_type not in _H3_TASK_MAP:
        return
    h3_task, frame_indices = _H3_TASK_MAP[task_type]
    # Merge, never replace: new-api forwards the caller's metadata verbatim, so
    # extra_params may already carry duration / audio_flow_shift / seed. Reassign
    # explicitly — the value may be a non-dict from a direct caller.
    extra_params = engine_body.get("extra_params")
    if not isinstance(extra_params, dict):
        extra_params = {}
    engine_body["extra_params"] = extra_params
    # A caller-supplied task wins, consistent with every other backfill here.
    extra_params.setdefault("task", h3_task)
    if frame_indices is not None:
        extra_params.setdefault("frame_indices", list(frame_indices))


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
        # Bernini-exclusive playstyles are meaningless on any other backend:
        # LightX2V/custom video engines don't know these task_type values, and
        # mv2v/ads2v additionally carry a comma-joined two-video src_video that a
        # single-video engine would read as one invalid path. Reject with a clean
        # 400 BEFORE admission / row creation instead of failing dirty downstream.
        if task_type in _BERNINI_TASK_TYPES and model.backend != BackendEnum.BERNINI:
            raise BadRequestException(
                message=(
                    f"task_type '{task_type}' is Bernini-only, but model "
                    f"'{model_name}' runs backend '{model.backend}'"
                ),
                is_openai_exception=True,
            )
        # Same guard for the H3-exclusive playstyles, and here silence is the
        # danger rather than a hard failure: l2va carries exactly one image, so a
        # LightX2V engine — which infers its mode from the input fields, not from
        # task_type — accepts it happily and renders a normal FIRST-frame i2v.
        # The user asked for "reverse from the last frame" and gets a plausible
        # video generated from the wrong end, with no error anywhere.
        # The H3 translation below is gated on the vLLM-Omni backend, so without
        # this check the request would reach the engine with no extra_params.task
        # and no frame_indices at all.
        #
        # The backend alone is NOT sufficient here: vLLM-Omni also runs the TTS
        # fleet, and _engine_kind dispatches purely on task_type, so l2va aimed at
        # a speech deployment would still be POSTed to its /v1/tasks/video/ and
        # fail deep inside the engine rather than here. Hence the second,
        # fail-CLOSED test — see _is_h3_video_deployment for why the polarity
        # differs from the ref2va guard below.
        if task_type in _H3_ONLY_TASK_TYPES and not (
            model.backend == BackendEnum.VLLM_OMNI and _is_h3_video_deployment(model)
        ):
            raise BadRequestException(
                message=(
                    f"task_type '{task_type}' needs a MiniMax-H3 video deployment, "
                    f"but model '{model_name}' runs backend '{model.backend}'. "
                    f"If this IS an H3 model, declare categories=[video] on it."
                ),
                is_openai_exception=True,
            )
        # Ref2VA-partition task types on a model we can positively identify as
        # FL2VA-only: reject at submit instead of letting it burn a queue slot
        # and a dispatch. Deliberately fail-OPEN on None — a wrong guess here
        # would refuse a working deployment, which is worse than the engine's
        # own (fast, 400, self-explanatory) rejection.
        #
        # Failing open is cheap and clean, which is what licenses it: the engine
        # raises OmniClientError for a partition mismatch, and vLLM-Omni defines
        # that class as "request-scoped, surfaced as 4xx". A 4xx takes the
        # err.status < 500 branch of the submit handler below, which DELETES the
        # task row before re-raising as 400 — so an undeployed task costs one
        # round trip and leaves nothing behind: no orphan row, no queue slot, and
        # never a silently mis-rendered video (the engine's shape inference would
        # also land on ref2va here, so even a lost extra_params.task still 400s).
        if (
            task_type in _H3_REF2VA_TASK_TYPES
            and model.backend == BackendEnum.VLLM_OMNI
            and _h3_ref2va_capability(model) is False
        ):
            raise BadRequestException(
                message=(
                    f"task_type '{task_type}' needs the MiniMax-H3 Ref2VA checkpoint, "
                    f"but model '{model_name}' is deployed from the FL2VA partition "
                    f"(serves t2va/fl2va only). Point this task_type at a Ref2VA deployment."
                ),
                is_openai_exception=True,
            )
        running = await ModelInstanceService(session).get_running_instances(model.id)
        # Backpressure: reject fast (429) before creating any row if the queue
        # for this model is already too deep (§4.1).
        #
        # Pass the RESOLVED ``model.name``, not the caller's ``model_name``: the
        # latter may be a model-route name / owner-prefixed alias / upstream
        # channel alias, which would miss the exact pass in _lookup_by_model and
        # fall through to substring guessing. Here the model is already resolved,
        # so the admission tables key off the authoritative name.
        await _check_admission(session, model.id, model.name, task_type, running)
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
    input_overrides = _resolve_input_refs(body.get("input_refs"), user_id, task_type)
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
    _backfill_h3_engine_params(engine_body, model.backend, task_type)
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
                # assigned_at stays NULL until the submit below succeeds: the
                # engine has accepted nothing yet, and this row exists only to
                # keep a crash from orphaning the job. Stamping it here would
                # date the attempt up to _SUBMIT_TIMEOUT before the engine
                # actually queued it, so two tasks racing onto the same
                # instance could be reported in the opposite order to the one
                # the engine will run them in. Within this window
                # _queue_counts reports "unknown", which is the contract's
                # documented answer for a task mid-dispatch.
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
        await task.update(
            session,
            {
                "native_task_id": native_task_id,
                # Lands with native_task_id, because they record the same
                # event: the engine took the job, so this attempt now holds a
                # place in that instance's FIFO. Same instant redispatch_task
                # uses, so both dispatch paths order by the same clock.
                "assigned_at": datetime.now(timezone.utc),
            },
        )
    logger.info(
        f"Video task {public_id} assigned to instance {instance.id} "
        f"(native={native_task_id}, model={model_name})"
    )
    return await _public_with_queue(task)


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
    # A field is "multi" only where the cap actually allows more than one: a
    # vace / v2v / rv2v upload with two videos, or an InfiniteTalk s2v with two
    # audio files, must be rejected here rather than comma-joined into one
    # invalid path downstream.
    cap = _input_cap(field, task_type)
    multi = cap > 1
    if len(files) > cap:
        raise BadRequestException(
            message=(
                f"Too many '{field}' files: {len(files)} (max {cap})"
                if multi
                else f"'{field}' accepts a single file, got {len(files)}"
            ),
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


# Substring that marks the engine's ONE retryable 503. vLLM-Omni's task manager
# raises RuntimeError("Task queue is full (max N tasks)") and the route maps it to
# 503; every other 503 it can emit is a permanent condition wearing the same
# status code. Matched lowercased and as a substring so the "(max N tasks)" tail
# and any future prefix do not break it.
_ENGINE_QUEUE_FULL = "queue is full"


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

    if resp.status == 503 and _ENGINE_QUEUE_FULL in (
        body_bytes.decode(errors="replace").lower() if body_bytes else ""
    ):
        # Engine per-instance FIFO is full — surface as backpressure so new-api
        # self-throttles (§6.0). least-pending already steered away from busy
        # instances; this is the residual overflow.
        #
        # Matched on the body, NOT on the bare status: the engine answers 503 for
        # a dozen other conditions (uninitialised handler for the requested
        # modality, duplicate task id, engine still warming up), none of which a
        # retry can fix. Calling those "busy" was actively harmful — the message
        # replaced the engine's own explanation, and kind="busy" is the one kind
        # _redispatch treats as neither permanent nor transient, so retry_count
        # never advanced and the sweeper re-submitted a doomed task forever.
        # Everything else now falls through to the generic branch: detail
        # preserved, kind="transient", retry_count bounded by _MAX_DISPATCH_RETRIES.
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
            # New attempt, new clock AND new bar: the dead run's progress must
            # not carry over, or an attempt that reached the estimate ceiling
            # freezes its successor at 95% for the whole re-run.
            #
            # Redundant today, and kept anyway: every row that reaches this
            # function has already been reset, because **no row is ever QUEUED
            # from birth**. The one insert path (create_video_task) writes
            # ASSIGNED, and the only two writers of QUEUED — the sweeper's
            # _requeue and the engine-404 fold in fetch_engine_status_updates —
            # each fold ATTEMPT_RESET in themselves. Repeating it keeps
            # re-dispatch correct on its own terms instead of by reading two
            # other call sites.
            #
            # Do not "simplify" by weakening that invariant: _queue_counts'
            # QUEUED branch counts ALL in-flight rows for the model rather than
            # only those created before the task, and that is only right
            # because a queued row's created_at is its ORIGINAL submission —
            # older than the tasks that took the instances while it was dead.
            # A new insert path that writes QUEUED would silently make the
            # queue report answer "starting now" to a task behind a full fleet.
            **ATTEMPT_RESET,
            # MUST stay after the spread: ATTEMPT_RESET clears assigned_at
            # (the dead attempt's queue slot is gone) and this new attempt's
            # slot is now. A dict literal keeps the LAST value for a repeated
            # key, so this line's position is load-bearing — moving it above
            # the spread would silently null the column and drop this task out
            # of the queue-position ordering.
            "assigned_at": datetime.now(timezone.utc),
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
            return await _public_with_queue(task, session)
        if not task.instance_id or not task.native_task_id:
            # Requeued and not yet re-dispatched by the sweeper.
            return await _public_with_queue(task, session)

        instance = await ModelInstanceService(session).get_by_id(task.instance_id)
        if not instance or instance.state != ModelInstanceStateEnum.RUNNING:
            # Instance is gone; leave the row for the death-requeue sweeper.
            return await _public_with_queue(task, session)
        worker: Worker = await WorkerService(session).get_by_id(instance.worker_id)
        if not worker:
            return await _public_with_queue(task, session)

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
    return await _public_with_queue(task)


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
            # This attempt is over: without the reset the caller would see
            # status="queued" next to the dead run's progress=80/phase="decode"
            # (queued is a fixed tier in the client, §3.4).
            **ATTEMPT_RESET,
        }
    if resp.status >= 400 or not body_bytes:
        return None

    try:
        status = json.loads(body_bytes)
    except Exception:
        return None

    new_state = _ENGINE_STATE_MAP.get(status.get("status"))
    if new_state is None:
        # Nothing to fold: an unrecognized status means we can't say what the row
        # should become, and progress without a state would be meaningless.
        return None
    updates: Dict[str, Any] = {"state": new_state}
    if new_state == VideoTaskStateEnum.FAILED:
        updates["state_message"] = status.get("error")
        updates["error_type"] = status.get("error_type") or None
    elif status.get("status") == "processing":
        updates["state_message"] = None
    updates.update(_progress_updates(task, status, new_state))
    return updates


def _progress_updates(
    task: VideoGenerationTask, status: Dict[str, Any], new_state: VideoTaskStateEnum
) -> Dict[str, Any]:
    """Progress fields to fold into the row update (see
    docs/视频任务进度上报-统一契约设计.md).

    DONE persists 100 rather than leaving the last running value: §3.4 makes
    ``progress`` 100 at ``done``, and ``_public`` is not the only reader —
    ``VideoTaskPublic`` serializes these columns raw for the management list, so
    a row left at 92.7 shows up there as "done · 92.7%".

    FAILED/CANCELED deliberately keep whatever they reached: "died at 82% in
    denoise" is the diagnostic, and the client treats terminal states as
    complete regardless. No write throttling — ``updates`` always carries
    ``state``, so the row is written on every poll regardless.
    """
    if new_state == VideoTaskStateEnum.DONE:
        # phase is cleared: a finished job has no current stage.
        return {"progress": 100.0, "phase": None}
    if new_state != VideoTaskStateEnum.RUNNING:
        return {}
    now = datetime.now(timezone.utc)
    updates: Dict[str, Any] = {}
    # The estimate is anchored on the first poll that OBSERVES the task running,
    # not on dispatch: the engine has its own queue, so a task can sit `pending`
    # for minutes and anchoring at dispatch would over-report — the costlier
    # failure (see the ceilings in video_progress). The anchor and the elapsed
    # it feeds come from one variable so they cannot disagree; on the first poll
    # that means elapsed=0, i.e. "just started", not "unknown".
    started = task.run_started_at or now
    if task.run_started_at is None:
        updates["run_started_at"] = started
    progress, phase, source = normalize_progress(
        status,
        kind=_engine_kind(task.task_type),
        prior=task.progress,
        elapsed=elapsed_seconds(started, now),
        expected_seconds=_model_latency(
            get_global_config(), task.model_name or "", task.task_type
        ),
    )
    logger.debug(
        "Task %s progress %.1f%% (phase=%s, source=%s)",
        task.task_id,
        progress,
        phase,
        source,
    )
    updates["progress"] = progress
    updates["phase"] = phase
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
            return await _public_with_queue(task, session)
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
    return await _public_with_queue(task)


def _public(task: VideoGenerationTask) -> Dict[str, Any]:
    """External contract: a stable public id, the lifecycle state, the internal
    ``nfs_path`` (new-api reads it directly off the shared mount), and the
    normalized progress.

    ``progress`` is authoritative only alongside ``status``: DONE is 100 by
    definition, and a failed job keeps whatever it had reached (the client marks
    terminal jobs complete on its own). ``_progress_updates`` now persists the
    100 on the DONE transition, so the coalesce below is a safety net for rows
    that reached DONE before that (and for the pre-migration backfill).
    """
    done = task.state == VideoTaskStateEnum.DONE
    return {
        "task_id": task.task_id,
        "status": task.state.value if task.state else None,
        "model": task.model_name,
        "task_type": task.task_type,
        "nfs_path": task.nfs_path if done else None,
        "error": task.state_message,
        "error_type": task.error_type,
        "progress": 100.0 if done else round(task.progress or 0.0, 1),
        "phase": task.phase,
    }


async def _public_with_queue(
    task: VideoGenerationTask, session: Optional[AsyncSession] = None
) -> Dict[str, Any]:
    """``_public`` plus the queue report. Every status-returning route goes
    through here so a caller never has to know which routes bothered to compute
    it.

    Pass ``session`` when one is already open. Without it this opens its own,
    and a caller that is already inside ``async with async_session()`` would
    hold two pooled connections at once — on the GET poll path, which every
    in-flight task hits every 15s, that doubles peak checkouts for one COUNT.
    """
    body = _public(task)
    body.update(await _queue_info(task, session))
    return body


async def _queue_info(
    task: VideoGenerationTask, session: Optional[AsyncSession] = None
) -> Dict[str, Any]:
    """How many generations must finish before this task starts, and roughly
    how long that is.

    ``queue_ahead`` is deliberately in units of *generations on this task's own
    path*, not "rows in the table". A model with 8 instances draining 8 jobs at
    once must not tell the 8th submitter "7 people ahead of you" when nobody is
    actually in front of them. So:

    - ASSIGNED — the task already sits in one instance's engine FIFO, and only
      that instance's earlier tasks block it. Tasks on the other seven
      instances are irrelevant, which makes this both the smaller number and
      the honest one.
    - QUEUED — no instance yet, so fall back to the fleet-wide estimate
      ``depth_ahead // instances``: the same arithmetic ``_check_admission``
      uses to decide whether to accept the job in the first place, so the
      number a client sees cannot contradict the reason it was let in.

    ``None`` means "cannot say" and the client should fall back to a plain
    "queued" message — never guessed at, because a wrong position is worse than
    no position.
    """
    unknown = {"queue_ahead": None, "estimated_start_seconds": None}
    if task.state in VIDEO_TASK_TERMINAL_STATES:
        return unknown
    if task.state == VideoTaskStateEnum.RUNNING:
        return {"queue_ahead": 0, "estimated_start_seconds": 0}

    cfg = get_global_config()
    # task.model_name is the CALLER-supplied string, which is why the lookup
    # below can only substring-match — see _lookup_by_model's docstring. This is
    # the poller case it describes, so table row order (longest key first)
    # matters for the value we get back.
    latency = _model_latency(cfg, task.model_name, task.task_type)

    if session is not None:
        return await _queue_counts(session, task, latency)
    async with async_session() as own_session:
        return await _queue_counts(own_session, task, latency)


async def _queue_counts(
    session: AsyncSession, task: VideoGenerationTask, latency: int
) -> Dict[str, Any]:
    """The two counting queries behind ``_queue_info``; split out only so the
    caller can decide where the session comes from."""
    unknown = {"queue_ahead": None, "estimated_start_seconds": None}
    if task.state == VideoTaskStateEnum.ASSIGNED:
        if task.instance_id is None or task.assigned_at is None:
            # Mid-dispatch, or a row that predates the assigned_at column
            # and was not caught by the backfill. No position to report.
            return unknown
        stmt = (
            select(func.count())
            .select_from(VideoGenerationTask)
            .where(
                VideoGenerationTask.instance_id == task.instance_id,
                # Enum MEMBERS, not .value: the ORM persists the member
                # NAME, so filtering by the lower-case value matches
                # nothing (same trap documented in
                # select_least_pending_instance).
                VideoGenerationTask.state.in_(
                    [VideoTaskStateEnum.ASSIGNED, VideoTaskStateEnum.RUNNING]
                ),
                VideoGenerationTask.assigned_at.is_not(None),
                VideoGenerationTask.assigned_at < task.assigned_at,
            )
        )
        ahead = int((await session.exec(stmt)).first() or 0)
        return {
            "queue_ahead": ahead,
            "estimated_start_seconds": ahead * latency,
        }

    # QUEUED: no instance yet, so the question is when a slot frees up.
    #
    # EVERY queued row is a requeued one — the insert path is born ASSIGNED
    # (see create_video_task) and only the sweeper's _requeue and the
    # engine-404 fold ever write QUEUED. So its created_at is the ORIGINAL
    # submission, older than the tasks that took the instances while this one
    # was dead: counting "rows created before me" would exclude exactly the
    # work that is blocking us, and answer 0 — which the contract reads as
    # "starting now" — for a task queued behind a fully busy fleet.
    #
    # What is really ahead of a task holding no slot:
    #   - every in-flight row on this model, whenever it was created, because
    #     each one occupies engine capacity we are waiting on;
    #   - queued peers created before us. A tiebreak, not a promise: the
    #     sweeper drains QUEUED in no particular order (no ORDER BY), so this
    #     is the fairest split available between rows that are all waiting.
    # floor(that / instances) is the arithmetic _check_admission used to let
    # the task in, over a subset of the rows it counted — so the number can
    # never exceed, and never contradict, the admission estimate.
    if task.model_id is None:
        return unknown
    instances = len(
        await ModelInstanceService(session).get_running_instances(task.model_id)
    )
    if instances <= 0:
        # Nothing is draining the queue; any number would be a fiction.
        return unknown
    stmt = (
        select(func.count())
        .select_from(VideoGenerationTask)
        .where(
            VideoGenerationTask.model_id == task.model_id,
            or_(
                VideoGenerationTask.state.in_(
                    [VideoTaskStateEnum.ASSIGNED, VideoTaskStateEnum.RUNNING]
                ),
                and_(
                    VideoGenerationTask.state == VideoTaskStateEnum.QUEUED,
                    VideoGenerationTask.created_at < task.created_at,
                ),
            ),
        )
    )
    depth_ahead = int((await session.exec(stmt)).first() or 0)
    ahead = depth_ahead // instances
    return {
        "queue_ahead": ahead,
        "estimated_start_seconds": ahead * latency,
    }
