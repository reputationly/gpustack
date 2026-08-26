"""Per-model admission table lookup (latency / queue-wait).

The table is keyed by model name with **exact match first, substring fallback**.
The regression these tests pin down: with substring-only matching, a shorter key
listed above a longer one silently shadows it — ``qwen-image`` swallowing
``qwen-image-edit`` meant the edit model's configured latency was dead config and
backpressure used the wrong number.
"""

from gpustack.routes.videos import (
    _DEFAULT_IMAGE_LATENCY,
    _DEFAULT_VIDEO_LATENCY,
    _lookup_by_model,
    _model_latency,
    _model_queue_wait,
)


class _Cfg:
    """Minimal stand-in for Config: only the two tables are read."""

    def __init__(self, latency=None, queue_wait=None):
        self.lightx2v_model_latency_seconds = latency
        self.lightx2v_model_queue_wait_seconds = queue_wait


# Row order deliberately puts the SHORT key first — the layout that used to break.
_SHADOWING_TABLE = {
    "qwen-image": 17,
    "qwen-image-edit": 21,
    "z-image": 9,
}


def test_exact_match_wins_over_earlier_substring_row():
    # Was 17 under substring-only matching (first row whose key is contained).
    assert _lookup_by_model(_SHADOWING_TABLE, "qwen-image-edit") == 21
    assert _lookup_by_model(_SHADOWING_TABLE, "qwen-image") == 17


def test_exact_match_is_case_insensitive():
    assert _lookup_by_model(_SHADOWING_TABLE, "QWEN-Image-Edit") == 21
    assert _lookup_by_model({"Z-Image": 9}, "z-image") == 9


def test_substring_fallback_still_serves_aliases():
    # The progress poller only has the caller-supplied string, which may be a
    # route name / owner-prefixed alias that equals no key exactly.
    assert _lookup_by_model({"qwen-image-edit": 21}, "acme/qwen-image-edit-v2") == 21
    assert _lookup_by_model(_SHADOWING_TABLE, "acme/z-image-v2") == 9


def test_substring_fallback_honours_row_order():
    # No exact hit, so the first *containing* key wins — order still matters for
    # aliases, which is why the UI keeps the reorder controls.
    table = {"qwen-image": 17, "qwen-image-edit": 21}
    assert _lookup_by_model(table, "vendor/qwen-image-edit") == 17


def test_unparseable_value_is_skipped_not_fatal():
    table = {"z-image": "not-a-number", "image": 12}
    assert _lookup_by_model(table, "z-image") == 12


def test_empty_and_missing_inputs():
    assert _lookup_by_model(None, "z-image") is None
    assert _lookup_by_model({}, "z-image") is None
    assert _lookup_by_model({"z-image": 9}, "") is None
    assert _lookup_by_model({"z-image": 9}, None) is None


def test_no_match_falls_back_to_per_kind_default():
    cfg = _Cfg(latency=_SHADOWING_TABLE)
    assert _model_latency(cfg, "brand-new-model", "t2i") == _DEFAULT_IMAGE_LATENCY
    assert _model_latency(cfg, "brand-new-model", "t2v") == _DEFAULT_VIDEO_LATENCY


def test_per_model_queue_wait_overrides_per_kind_ceiling():
    cfg = _Cfg(queue_wait={"hunyuan-image-3": 260})
    cfg.lightx2v_image_max_queue_wait_seconds = 25
    assert _model_queue_wait(cfg, "hunyuan-image-3", "t2i") == 260
    # Unconfigured model keeps the per-kind ceiling.
    assert _model_queue_wait(cfg, "z-image", "t2i") == 25


def test_both_tables_share_matching_semantics():
    # Same keys, same shadowing layout: the queue-wait table must not regress to
    # substring-first while the latency table is exact-first.
    cfg = _Cfg(
        latency={"qwen-image": 17, "qwen-image-edit": 21},
        queue_wait={"qwen-image": 40, "qwen-image-edit": 90},
    )
    assert _model_latency(cfg, "qwen-image-edit", "i2i") == 21
    assert _model_queue_wait(cfg, "qwen-image-edit", "i2i") == 90
