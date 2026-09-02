"""Engine-agnostic progress normalization for async generation tasks.

Engines report "which phase am I in and how far through it" (or nothing at all);
this module folds that into a single global 0-100 the facade stores and hands to
new-api. Keeping the fold here — rather than asking every engine to compute a
global percentage — is what makes a new engine's adaptation a matter of emitting
two optional fields instead of reasoning about its own stage cost model.

See docs/视频任务进度上报-统一契约设计.md for the full contract.

Deliberately free of any ``gpustack.routes`` import: ``routes/videos.py`` imports
this module, and the engine kind / expected latency it needs are passed in by the
caller (both come from helpers that live in that module).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Controlled phase vocabulary, in execution order. An engine that reports
# anything else is treated as "no phase" — an open vocabulary would let a typo
# ("denoising") silently fold to the wrong weight.
PHASE_ORDER: Tuple[str, ...] = ("prepare", "encode", "denoise", "decode", "save")

# Per-engine-kind stage cost model, keyed by the facade's own ``_engine_kind()``
# vocabulary. Weights are relative (normalized before use), so a table that does
# not sum to 100 still behaves.
#
# The video numbers come from MiniMax-H3 at 768p/15s: sampling dominates, but VAE
# decode is minutes-scale on long clips and ref2va spends real CPU time demuxing
# reference videos before a single GPU op runs. Audio/music engines are
# effectively "encode then sample then write", so their tables collapse the
# stages they never report.
PHASE_WEIGHTS: dict[str, dict[str, float]] = {
    "video": {"prepare": 8, "encode": 12, "denoise": 60, "decode": 15, "save": 5},
    "image": {"prepare": 4, "encode": 10, "denoise": 76, "decode": 8, "save": 2},
    "audio": {"prepare": 5, "encode": 10, "denoise": 75, "decode": 5, "save": 5},
    "music": {"prepare": 5, "encode": 10, "denoise": 70, "decode": 10, "save": 5},
    "audiogen": {"prepare": 5, "encode": 10, "denoise": 70, "decode": 10, "save": 5},
}
_DEFAULT_WEIGHTS = PHASE_WEIGHTS["video"]

# Running-task ceilings. 100 is reserved for the terminal state, and an estimate
# is held further back than an engine-reported value on purpose: when the guess
# is wrong, a bar parked at 99% for minutes reads as "hung", while 95% reads as
# "nearly there". Under-promising is the cheaper failure.
ENGINE_CEILING = 99.0
ESTIMATE_CEILING = 95.0

# Progress sources, returned alongside the value for logging/telemetry.
SOURCE_ENGINE = "engine"
SOURCE_ESTIMATE = "estimate"
SOURCE_NONE = "none"

# One attempt's progress dies with that attempt. ``normalize_progress`` is
# monotonic against ``prior`` to absorb source flapping *within* a run; carrying
# that high-water mark across a re-dispatch would pin the retry at the dead
# run's value — an attempt that reached ESTIMATE_CEILING would freeze its
# successor at 95% for the entire re-run, which is exactly the "reads as hung"
# failure the ceilings above exist to avoid. Every caller that ends an attempt
# (requeue, re-dispatch) folds this in.
#
# ``assigned_at`` rides along for the same reason: it marks the attempt's place
# in an instance's engine queue, and a dead attempt holds no place. Leaving a
# stale value behind would put a requeued task's *old* position into the queue
# report while it is waiting for a fresh instance. A caller that STARTS a new
# attempt must set it again after folding this in — see redispatch_task.
ATTEMPT_RESET: dict[str, Any] = {
    "progress": 0.0,
    "phase": None,
    "run_started_at": None,
    "assigned_at": None,
}


def elapsed_seconds(
    run_started_at: Optional[datetime], now: datetime
) -> Optional[float]:
    """Seconds a task has been running, or None when it hasn't started.

    Rows loaded from SQLite come back naive; treat those as UTC (which is what
    UTCDateTime stored) so the subtraction doesn't blow up on mixed awareness.
    """
    if run_started_at is None:
        return None
    started = (
        run_started_at
        if run_started_at.tzinfo
        else run_started_at.replace(tzinfo=timezone.utc)
    )
    return max(0.0, (now - started).total_seconds())


def _as_float(value: Any) -> Optional[float]:
    """Coerce an engine-supplied number, rejecting bools and junk."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return result


def _clamp(value: float, ceiling: float = 100.0) -> float:
    return max(0.0, min(ceiling, value))


def _weights(kind: str) -> dict[str, float]:
    return PHASE_WEIGHTS.get(kind or "", _DEFAULT_WEIGHTS)


def _known_phase(payload: Mapping[str, Any]) -> Optional[str]:
    raw = payload.get("phase")
    if not isinstance(raw, str):
        return None
    phase = raw.strip().lower()
    if phase in PHASE_ORDER:
        return phase
    if phase:
        logger.debug("Ignoring unknown progress phase %r", raw)
    return None


def fold_phase(kind: str, phase: str, phase_progress: float) -> float:
    """Global 0-100 for ``phase_progress`` percent through ``phase``: every
    preceding phase's weight plus this phase's share."""
    weights = _weights(kind)
    total = sum(weights.values()) or 100.0
    done = sum(weights.get(p, 0.0) for p in PHASE_ORDER[: PHASE_ORDER.index(phase)])
    share = weights.get(phase, 0.0) * _clamp(phase_progress) / 100.0
    return (done + share) * 100.0 / total


def _engine_value(
    payload: Mapping[str, Any], kind: str, phase: Optional[str]
) -> Optional[float]:
    """Engine-reported progress, in contract priority order. Returns None when
    the engine said nothing usable."""
    # 1. A global percentage the engine folded itself — it knows its own stage
    #    costs better than our table does, so it wins outright.
    reported = _as_float(payload.get("progress"))
    if reported is not None and reported > 0:
        return _clamp(reported)

    if phase is None:
        return None

    # 2. Phase + percentage within it.
    phase_progress = _as_float(payload.get("phase_progress"))
    if phase_progress is not None:
        return fold_phase(kind, phase, phase_progress)

    # 3. Raw step counters — sugar for phase_progress, the cheapest thing an
    #    engine can bolt onto its sampling loop.
    step = _as_float(payload.get("step"))
    total_steps = _as_float(payload.get("total_steps"))
    if step is not None and total_steps is not None and total_steps > 0:
        return fold_phase(kind, phase, step * 100.0 / total_steps)
    return None


def normalize_progress(
    payload: Mapping[str, Any],
    *,
    kind: str,
    prior: float = 0.0,
    elapsed: Optional[float] = None,
    expected_seconds: Optional[float] = None,
) -> Tuple[float, Optional[str], str]:
    """Fold one engine status payload into ``(progress, phase, source)``.

    ``prior`` is the value already stored for the task: progress is forced
    monotonic against it, but only *within* one attempt. Sources flap — an engine
    that reports a percentage on one poll and goes silent on the next would drop
    the bar to the (lower) elapsed-time estimate, and a bar that walks backwards
    mid-run reads as a bug. Across attempts the opposite is true: a re-dispatch
    restarts the engine's counters at zero, so the caller clears the row first
    (see ATTEMPT_RESET) and this fold starts from 0 again.

    ``elapsed``/``expected_seconds`` drive the fallback used for engines that
    report nothing (see ESTIMATE_CEILING for why it is capped lower).
    """
    prior = _clamp(_as_float(prior) or 0.0)
    phase = _known_phase(payload)

    value = _engine_value(payload, kind, phase)
    if value is not None:
        return max(_clamp(value, ENGINE_CEILING), prior), phase, SOURCE_ENGINE

    elapsed = _as_float(elapsed)
    expected = _as_float(expected_seconds)
    if elapsed is not None and elapsed >= 0 and expected is not None and expected > 0:
        estimate = _clamp(elapsed * 100.0 / expected, ESTIMATE_CEILING)
        return max(estimate, prior), phase, SOURCE_ESTIMATE

    return prior, phase, SOURCE_NONE
