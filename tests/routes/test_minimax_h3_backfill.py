"""MiniMax-H3 facade wiring.

Everything locked here fails SILENTLY in production if it regresses:

- ``extra_params`` is a NESTED key. ``VideoGenerationRequest`` has no
  ``extra="forbid"``, so a top-level ``task`` is dropped without an error and the
  engine falls back to inferring the task from the media shape — which cannot
  tell ``fl2va``+``frame_indices=[-1]`` from a single-image ``ref2va``.
- ``frame_indices`` is the ONLY thing distinguishing first-frame from last-frame
  on one FL2VA checkpoint. Lose it and a "last frame" request quietly renders as
  a first-frame one.
- The vLLM-Omni category default used to be a flat ``TEXT_TO_SPEECH``, which
  filed H3 under the speech playground.
"""

import pytest

from gpustack.routes.videos import (
    _BERNINI_TASK_TYPES,
    _H3_ONLY_TASK_TYPES,
    _H3_TASK_MAP,
    _VALID_TASK_TYPES,
    _VIDEO_TASK_TYPES,
    _backfill_h3_engine_params,
    _h3_ref2va_capability,
    _H3_REF2VA_TASK_TYPES,
    _ENGINE_QUEUE_FULL,
    _engine_kind,
    _input_cap,
    _is_h3_video_deployment,
)
from gpustack.schemas.models import BackendEnum, CategoryEnum, Model, SourceEnum
from gpustack.scheduler.scheduler import _vllm_omni_category


# ── task_type vocabulary ────────────────────────────────────────────────────


def test_l2va_is_a_valid_video_task_type():
    # Must stay in lockstep with new-api's validTaskTypes and gpustack-ui's
    # KNOWN_TASK_TYPES; an unlisted value is rejected outright by the facade.
    assert "l2va" in _VIDEO_TASK_TYPES
    assert "l2va" in _VALID_TASK_TYPES


def test_l2va_routes_to_the_video_engine_kind():
    # _engine_kind decides which /v1/tasks/{kind}/ endpoint the job is POSTed to.
    assert _engine_kind("l2va") == "video"


# ── H3 task mapping ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "task_type,want_task,want_indices",
    [
        ("t2v", "t2va", None),
        ("i2v", "fl2va", [0]),
        ("l2va", "fl2va", [-1]),
        ("flf2v", "fl2va", [0, -1]),
        ("s2v", "ref2va", None),
    ],
)
def test_h3_task_map(task_type, want_task, want_indices):
    assert _H3_TASK_MAP[task_type] == (want_task, want_indices)


def test_h3_frame_indices_are_the_three_the_engine_accepts():
    # pipeline_minimax_h3 accepts exactly [0], [-1] and [0, -1], and requires
    # len(frame_indices) == len(images). Anything else is a hard 400.
    indices = {tuple(idx) for _, idx in _H3_TASK_MAP.values() if idx is not None}
    assert indices == {(0,), (-1,), (0, -1)}


def test_i2v_and_l2va_differ_only_by_frame_indices():
    # Same engine task, same one-image input shape — the index is the entire
    # difference. This is why l2va has to be its own facade task_type rather than
    # something inferred from the request.
    assert _H3_TASK_MAP["i2v"][0] == _H3_TASK_MAP["l2va"][0] == "fl2va"
    assert _H3_TASK_MAP["i2v"][1] != _H3_TASK_MAP["l2va"][1]


def test_every_h3_task_type_is_a_valid_facade_task_type():
    for task_type in _H3_TASK_MAP:
        assert task_type in _VALID_TASK_TYPES


# ── backend exclusivity ─────────────────────────────────────────────────────


def test_h3_exclusive_set_excludes_shared_names():
    # The exclusive set is exactly the task types no other engine understands:
    #   l2va — introduced by H3, indistinguishable from i2v by input shape
    #   r2va — mixed-reference Ref2VA
    # t2v / i2v / flf2v / s2v are SHARED with LightX2V and InfiniteTalk; gating
    # those on the backend would break every existing Wan/InfiniteTalk request.
    assert _H3_ONLY_TASK_TYPES == {"l2va", "r2va"}
    for shared in ("t2v", "i2v", "flf2v", "s2v"):
        assert shared not in _H3_ONLY_TASK_TYPES


def test_h3_exclusive_types_do_not_overlap_bernini():
    assert not (_H3_ONLY_TASK_TYPES & _BERNINI_TASK_TYPES)


def test_l2va_admission_is_guarded_on_the_backend():
    """l2va on a non-H3 video model must be rejected, not silently mis-rendered.

    LightX2V infers its mode from the input fields rather than task_type, so a
    one-image l2va request would render as a first-frame i2v — a plausible video
    generated from the wrong end of the clip, with no error raised anywhere.
    """
    import inspect

    from gpustack.routes.videos import create_video_task

    src = inspect.getsource(create_video_task)
    assert "_H3_ONLY_TASK_TYPES" in src
    assert "BackendEnum.VLLM_OMNI" in src
    # The backend test alone is not enough — vLLM-Omni also runs the TTS fleet.
    assert "_is_h3_video_deployment" in src


# ── is this vLLM-Omni deployment an H3 VIDEO model? ─────────────────────────


def _omni(name="m", source_key=None, argv=None, categories=None):
    m = Model(
        name=name,
        source=SourceEnum.LOCAL_PATH,
        local_path=source_key or "/nfs-data/models/x",
        backend=BackendEnum.VLLM_OMNI,
        backend_parameters=argv,
        categories=categories or [],
    )
    return m


@pytest.mark.parametrize(
    "source_key",
    [
        "/nfs-data/models/MiniMax-H3-FL2VA-INT8",
        "/nfs-data/models/MiniMax-H3/Ref2VA",
        "/nfs-data/models/minimax_h3",
    ],
)
def test_h3_deployments_are_recognised_from_the_weight_path(source_key):
    assert _is_h3_video_deployment(_omni(source_key=source_key)) is True


@pytest.mark.parametrize(
    "name",
    ["VoxCPM2", "CosyVoice3", "Qwen3-TTS", "MOSS-TTSD"],
)
def test_speech_deployments_on_the_same_backend_are_rejected(name):
    """The whole point of the guard: vLLM-Omni is NOT single-modality.

    _engine_kind dispatches on task_type alone, so without this an l2va aimed at
    a TTS deployment is POSTed to its /v1/tasks/video/ — which exists on every
    vLLM-Omni server — and dies deep in the engine instead of here.
    """
    model = _omni(name=name, source_key=f"/nfs-data/models/{name}")
    assert _is_h3_video_deployment(model) is False


def test_declared_video_category_is_the_escape_hatch():
    # An H3 checkpoint in a directory carrying none of the tokens: the operator
    # can force admission with categories=[video] instead of renaming weights.
    model = _omni(source_key="/nfs-data/models/omni-video-0925", categories=["video"])
    assert _is_h3_video_deployment(model) is True


def test_launch_argv_counts_as_evidence():
    # --task-type ref2va on an otherwise unrecognisable path.
    model = _omni(
        source_key="/nfs-data/models/omni-0925",
        argv=["--task-type", "ref2va"],
    )
    assert _is_h3_video_deployment(model) is True


def test_speech_model_is_not_rescued_by_a_tts_category():
    model = _omni(name="Qwen3-TTS", categories=["text_to_speech"])
    assert _is_h3_video_deployment(model) is False


# ── engine 503 is not automatically backpressure ────────────────────────────


def test_only_a_full_queue_counts_as_backpressure():
    """kind="busy" is the one kind _redispatch treats as neither permanent nor
    transient, so retry_count never advances and the sweeper retries forever.
    Handing that kind to a permanent 503 (uninitialised video handler on a speech
    deployment, duplicate task id) is what made a misroute unkillable."""
    assert _ENGINE_QUEUE_FULL in "Task queue is full (max 8 tasks)".lower()
    for permanent in (
        "Video generation handler not initialized.",
        "Task ID video_task_abc already exists",
    ):
        assert _ENGINE_QUEUE_FULL not in permanent.lower()


# ── the backfill itself ─────────────────────────────────────────────────────


def _backfill(engine_body: dict, backend, task_type: str) -> dict:
    """Exercise the REAL backfill used by create_video_task.

    Deliberately not a re-implementation: a copy of the logic would keep passing
    while the route drifted away from it.
    """
    _backfill_h3_engine_params(engine_body, backend, task_type)
    return engine_body


def test_backfill_writes_into_nested_extra_params():
    body = _backfill({}, BackendEnum.VLLM_OMNI, "l2va")
    assert body["extra_params"] == {"task": "fl2va", "frame_indices": [-1]}
    # A top-level `task` would be silently dropped by the engine.
    assert "task" not in body


def test_backfill_preserves_caller_supplied_extra_params():
    # new-api forwards the caller's metadata verbatim, so duration / seed /
    # audio_flow_shift are commonly already present. Merging, not replacing, is
    # the whole point.
    body = _backfill(
        {"extra_params": {"duration": 8.0, "audio_flow_shift": 3.0}},
        BackendEnum.VLLM_OMNI,
        "flf2v",
    )
    assert body["extra_params"] == {
        "duration": 8.0,
        "audio_flow_shift": 3.0,
        "task": "fl2va",
        "frame_indices": [0, -1],
    }


def test_caller_supplied_task_wins():
    # Consistent with the Bernini / AudioX backfills, which all use setdefault.
    body = _backfill(
        {"extra_params": {"task": "t2va", "frame_indices": [0]}},
        BackendEnum.VLLM_OMNI,
        "flf2v",
    )
    assert body["extra_params"]["task"] == "t2va"
    assert body["extra_params"]["frame_indices"] == [0]


def test_non_dict_extra_params_is_replaced_not_crashed():
    # A direct caller can send anything; the facade does not validate upstream.
    body = _backfill({"extra_params": "oops"}, BackendEnum.VLLM_OMNI, "t2v")
    assert body["extra_params"] == {"task": "t2va"}


def test_t2v_gets_no_frame_indices():
    body = _backfill({}, BackendEnum.VLLM_OMNI, "t2v")
    assert body["extra_params"] == {"task": "t2va"}


def test_backfill_does_not_touch_other_backends():
    # LightX2V serves t2v/i2v/flf2v under the same names and does NOT understand
    # extra_params.task — injecting it there would be a regression.
    for backend in (BackendEnum.LIGHTX2V, BackendEnum.BERNINI):
        body = _backfill({}, backend, "i2v")
        assert body == {}


def test_backfill_ignores_non_h3_task_types_on_vllm_omni():
    for task_type in ("sr", "v2a", "vace"):
        body = _backfill({}, BackendEnum.VLLM_OMNI, task_type)
        assert body == {}


def test_route_actually_calls_the_backfill():
    """The tests above exercise _backfill_h3_engine_params directly; make sure
    create_video_task is what actually invokes it."""
    import inspect

    from gpustack.routes.videos import create_video_task

    src = inspect.getsource(create_video_task)
    assert "_backfill_h3_engine_params(engine_body, model.backend, task_type)" in src


# ── category inference ──────────────────────────────────────────────────────


def _model(name: str, local_path: str) -> Model:
    return Model(
        name=name,
        source=SourceEnum.LOCAL_PATH,
        local_path=local_path,
        backend=BackendEnum.VLLM_OMNI,
    )


@pytest.mark.parametrize(
    "name,path,want",
    [
        (
            "minimax-h3-fl2va",
            "/nfs-data/models/MiniMax-H3-FL2VA-INT8",
            CategoryEnum.VIDEO,
        ),
        ("h3-video", "/nfs-data/models/MiniMax_H3/Ref2VA", CategoryEnum.VIDEO),
        # The pre-H3 vLLM-Omni fleet must stay exactly where it is.
        ("qwen3-tts", "Qwen/Qwen3-TTS", CategoryEnum.TEXT_TO_SPEECH),
        ("voxcpm2", "/nfs-data/models/VoxCPM2", CategoryEnum.TEXT_TO_SPEECH),
        ("audiox", "/nfs-data/models/AudioX", CategoryEnum.TEXT_TO_SPEECH),
        ("moss-ttsd", "/nfs-data/models/MOSS-TTSD", CategoryEnum.TEXT_TO_SPEECH),
    ],
)
def test_vllm_omni_category_inference(name, path, want):
    assert _vllm_omni_category(_model(name, path)) == want


# ── mixed-reference caps (public API must not be capped below the engine) ────


def test_r2va_cap_matches_the_engine():
    # pipeline_minimax_h3._validate_ref2va_reference_counts: <=9 images,
    # <=3 videos, <=3 standalone audio, <=12 total.
    assert _input_cap("image", "r2va") == 9
    assert _input_cap("video", "r2va") == 3
    assert _input_cap("audio", "r2va") == 3


def test_multi_audio_and_video_are_scoped_to_r2va():
    """Every other consumer of these fields contracts exactly one file.

    InfiniteTalk s2v takes one driving audio, SeedVR2 sr one source video; a
    comma-joined pair would reach those engines as a single invalid path.
    """
    for task_type in ("s2v", "sr", "v2a", "tts", "cover"):
        assert _input_cap("audio", task_type) == 1
        assert _input_cap("video", task_type) == 1


def test_default_image_cap_is_unchanged_for_other_tasks():
    # Raising the r2va cap must not loosen Bernini / image-edit paths.
    for task_type in ("i2i", "rv2v", "r2v"):
        assert _input_cap("image", task_type) == 5


def test_src_video_scoping_still_holds():
    # Pre-existing behaviour: two source videos only for the Bernini dual modes.
    assert _input_cap("src_video", "mv2v") == 2
    assert _input_cap("src_video", "ads2v") == 2
    assert _input_cap("src_video", "v2v") == 1
    assert _input_cap("src_video", "rv2v") == 1


def test_r2va_maps_to_the_same_engine_task_as_s2v():
    # Both are Ref2VA; they differ only in which references the caller may send.
    assert _H3_TASK_MAP["r2va"][0] == _H3_TASK_MAP["s2v"][0] == "ref2va"
    # Neither carries frame_indices — that is an FL2VA-only concept.
    assert _H3_TASK_MAP["r2va"][1] is None


# ── Ref2VA partition capability ─────────────────────────────────────────────


def _h3_model(name: str, path: str, argv=None) -> Model:
    return Model(
        name=name,
        source=SourceEnum.LOCAL_PATH,
        local_path=path,
        backend=BackendEnum.VLLM_OMNI,
        backend_parameters=argv,
    )


def test_ref2va_task_types():
    # Both map to extra_params.task='ref2va', so both need that partition.
    assert _H3_REF2VA_TASK_TYPES == {"s2v", "r2va"}


@pytest.mark.parametrize(
    "name,path,argv,want",
    [
        # --task-type is authoritative when present.
        ("h3", "/nfs-data/models/MiniMax-H3", ["--task-type", "ref2va"], True),
        ("h3", "/nfs-data/models/MiniMax-H3", ["--task-type", "fl2va"], False),
        # Production passes no --task-type; the weight path decides.
        ("a", "/nfs-data/models/MiniMax-H3/Ref2VA", None, True),
        ("b", "/nfs-data/models/MiniMax-H3-FL2VA-INT8", None, False),
        ("c", "/nfs-data/models/MiniMax-H3/FL2VA", None, False),
    ],
)
def test_ref2va_capability_from_deployment(name, path, argv, want):
    assert _h3_ref2va_capability(_h3_model(name, path, argv)) is want


def test_capability_is_none_without_evidence():
    """No evidence must mean "allow", never "reject".

    Combined mode loads BOTH partitions from the plain MiniMax-H3 directory, and
    an unrecognised layout is not proof of anything. Returning False here would
    refuse a working deployment — strictly worse than the engine's own fast 400.
    """
    assert _h3_ref2va_capability(_h3_model("h3", "/nfs-data/models/MiniMax-H3")) is None
    assert _h3_ref2va_capability(_h3_model("x", "")) is None


def test_capability_ignores_the_display_name():
    # The display name is operator-chosen; only what was actually loaded counts.
    m = _h3_model("minimax-h3-ref2va", "/nfs-data/models/MiniMax-H3-FL2VA-INT8")
    assert _h3_ref2va_capability(m) is False


def test_guard_rejects_only_on_positive_fl2va_evidence():
    import inspect

    from gpustack.routes.videos import create_video_task

    src = inspect.getsource(create_video_task)
    assert "_h3_ref2va_capability(model) is False" in src
