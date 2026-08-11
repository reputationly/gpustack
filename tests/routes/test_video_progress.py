"""Progress normalization (docs/视频任务进度上报-统一契约设计.md).

What is locked here is the behaviour a client actually notices:

- an engine that reports nothing must still show movement (estimate), or every
  self-hosted job looks hung at whatever fixed value the client picked;
- progress must never walk backwards *within* one attempt, because sources flap
  (an engine that reports a percentage on one poll and goes silent on the next
  would drop the bar to the lower estimate) and a shrinking bar reads as a bug;
- but it must reset *across* attempts, or a run that died at the estimate
  ceiling freezes its retry at 95% for the whole re-run;
- a running job must never claim 100 — that is the terminal state's value, and a
  client that treats it as "done but no result" is exactly the confusion this
  whole contract exists to remove;
- a job that reached DONE must persist 100, because ``_public`` is not the only
  reader: the management list serializes the columns raw.
"""

from datetime import datetime, timedelta, timezone

from gpustack.routes.videos import _progress_updates
from gpustack.schemas.video_generation_task import (
    VideoGenerationTask,
    VideoTaskBase,
    VideoTaskStateEnum,
)
from gpustack.server.video_progress import (
    ATTEMPT_RESET,
    ENGINE_CEILING,
    ESTIMATE_CEILING,
    PHASE_ORDER,
    SOURCE_ENGINE,
    SOURCE_ESTIMATE,
    SOURCE_NONE,
    elapsed_seconds,
    fold_phase,
    normalize_progress,
)


# ── source priority ─────────────────────────────────────────────────────────


def test_engine_global_progress_wins_over_everything_else():
    # The engine knows its own stage costs better than our weight table does.
    payload = {"progress": 42.5, "phase": "prepare", "phase_progress": 100}
    progress, phase, source = normalize_progress(payload, kind="video")
    assert (progress, phase, source) == (42.5, "prepare", SOURCE_ENGINE)


def test_phase_and_phase_progress_fold_through_the_weight_table():
    # video weights: prepare 8 + encode 12 = 20 done, denoise 60 * 50% = 30.
    progress, phase, source = normalize_progress(
        {"phase": "denoise", "phase_progress": 50}, kind="video"
    )
    assert (round(progress, 1), phase, source) == (50.0, "denoise", SOURCE_ENGINE)


def test_step_counters_are_sugar_for_phase_progress():
    by_steps, _, _ = normalize_progress(
        {"phase": "denoise", "step": 16, "total_steps": 32}, kind="video"
    )
    by_percent, _, _ = normalize_progress(
        {"phase": "denoise", "phase_progress": 50}, kind="video"
    )
    assert by_steps == by_percent


def test_silent_engine_falls_back_to_the_elapsed_time_estimate():
    progress, phase, source = normalize_progress(
        {"status": "processing"}, kind="video", elapsed=45, expected_seconds=90
    )
    assert (progress, phase, source) == (50.0, None, SOURCE_ESTIMATE)


def test_no_engine_data_and_no_clock_holds_the_prior_value():
    progress, _, source = normalize_progress({}, kind="video", prior=37.0)
    assert (progress, source) == (37.0, SOURCE_NONE)


def test_unknown_phase_is_ignored_rather_than_folded():
    # An open vocabulary would let a typo ("denoising") fold to the wrong weight.
    progress, phase, source = normalize_progress(
        {"phase": "denoising", "phase_progress": 90},
        kind="video",
        elapsed=9,
        expected_seconds=90,
    )
    assert (progress, phase, source) == (10.0, None, SOURCE_ESTIMATE)


def test_zero_progress_is_not_treated_as_a_report():
    # An engine that always sends progress=0 (field present, never filled) must
    # not suppress the estimate.
    progress, _, source = normalize_progress(
        {"progress": 0}, kind="video", elapsed=45, expected_seconds=90
    )
    assert (progress, source) == (50.0, SOURCE_ESTIMATE)


# ── monotonicity and ceilings ───────────────────────────────────────────────


def test_progress_never_walks_backwards_within_an_attempt():
    # Sources flap mid-run: an engine that reported a percentage can fall back to
    # phase-only (or nothing) on the next poll. Across attempts the caller resets
    # the row instead — see the ATTEMPT_RESET tests below.
    progress, _, _ = normalize_progress(
        {"phase": "prepare", "phase_progress": 0}, kind="video", prior=64.0
    )
    assert progress == 64.0


def test_estimate_never_passes_a_higher_engine_value():
    progress, _, _ = normalize_progress(
        {}, kind="video", prior=80.0, elapsed=45, expected_seconds=90
    )
    assert progress == 80.0


def test_running_engine_progress_is_capped_below_complete():
    progress, _, _ = normalize_progress({"progress": 100}, kind="video")
    assert progress == ENGINE_CEILING < 100


def test_estimate_is_capped_lower_than_engine_progress():
    # An overrunning job parks at 95, not 99: a bar pinned just short of done
    # for minutes reads as hung.
    progress, _, _ = normalize_progress(
        {}, kind="video", elapsed=10_000, expected_seconds=90
    )
    assert progress == ESTIMATE_CEILING < ENGINE_CEILING


def test_junk_engine_values_do_not_crash_or_leak_through():
    for junk in ("", "abc", None, float("nan"), float("inf"), True):
        progress, _, source = normalize_progress(
            {"progress": junk}, kind="video", elapsed=9, expected_seconds=90
        )
        assert (progress, source) == (10.0, SOURCE_ESTIMATE), junk


# ── attempt boundaries ──────────────────────────────────────────────────────


def test_attempt_reset_clears_every_field_the_fold_reads():
    # Anything the fold carries forward has to be listed here, or a dead
    # attempt's state leaks into its retry. run_started_at especially: leaving it
    # set makes the retry's first poll look minutes old.
    assert ATTEMPT_RESET == {"progress": 0.0, "phase": None, "run_started_at": None}


def test_attempt_reset_fields_exist_on_the_row():
    # Guards against a rename silently turning the reset into a no-op — the dict
    # is splatted into an update, which accepts unknown keys quietly.
    for field in ATTEMPT_RESET:
        assert field in VideoTaskBase.model_fields


def test_a_reset_attempt_starts_the_bar_over():
    payload = {"phase": "prepare", "phase_progress": 0}
    before, _, _ = normalize_progress(payload, kind="video", prior=95.0)
    after, _, _ = normalize_progress(
        payload, kind="video", prior=ATTEMPT_RESET["progress"]
    )
    assert before == 95.0
    assert after == 0.0


# ── row updates ─────────────────────────────────────────────────────────────


def _task(**kwargs) -> VideoGenerationTask:
    defaults = dict(task_id="task_x", task_type="t2v", model_name="wan2.2-t2v")
    return VideoGenerationTask(**{**defaults, **kwargs})


def test_done_persists_a_full_bar_and_no_phase():
    updates = _progress_updates(
        _task(progress=92.7, phase="decode"), {}, VideoTaskStateEnum.DONE
    )
    assert updates == {"progress": 100.0, "phase": None}


def test_failure_and_cancellation_keep_the_progress_they_died_at():
    # "died at 82% in denoise" is the diagnostic; the client marks terminal
    # states complete on its own anyway.
    for state in (VideoTaskStateEnum.FAILED, VideoTaskStateEnum.CANCELED):
        assert (
            _progress_updates(_task(progress=82.0, phase="denoise"), {}, state) == {}
        ), state


def test_queued_reports_nothing():
    assert _progress_updates(_task(), {}, VideoTaskStateEnum.QUEUED) == {}


def test_first_running_poll_anchors_the_clock_and_folds_the_engine_payload():
    updates = _progress_updates(
        _task(),
        {"phase": "denoise", "step": 16, "total_steps": 32},
        VideoTaskStateEnum.RUNNING,
    )
    assert updates["run_started_at"] is not None
    assert (round(updates["progress"], 1), updates["phase"]) == (50.0, "denoise")


def test_running_poll_of_a_silent_engine_estimates_from_the_anchor():
    started = datetime.now(timezone.utc) - timedelta(seconds=45)
    updates = _progress_updates(
        _task(run_started_at=started), {}, VideoTaskStateEnum.RUNNING
    )
    # Default video latency is 90s (no model latency table configured in tests),
    # and the anchor is not re-stamped once set.
    assert "run_started_at" not in updates
    assert 49 <= updates["progress"] <= 51


def test_a_silent_engine_reports_zero_on_the_poll_that_anchors_the_clock():
    # elapsed is measured from the anchor being stamped by THIS poll, so the
    # first observation is "just started" (0), never "unknown".
    updates = _progress_updates(
        _task(), {"status": "processing"}, VideoTaskStateEnum.RUNNING
    )
    assert updates["progress"] == 0.0


def test_attempt_reset_clears_every_field_a_running_poll_writes():
    # The invariant that keeps the reset honest as columns are added: if a future
    # poll writes a fourth progress field, ATTEMPT_RESET has to clear it too, or
    # the retry silently inherits it.
    written = set(
        _progress_updates(_task(), {"status": "processing"}, VideoTaskStateEnum.RUNNING)
    )
    assert written <= set(ATTEMPT_RESET)


# ── weight table ────────────────────────────────────────────────────────────


def test_phases_fold_in_execution_order_and_span_the_full_range():
    values = [fold_phase("video", phase, 0) for phase in PHASE_ORDER]
    assert values == sorted(values)
    assert values[0] == 0
    assert fold_phase("video", PHASE_ORDER[-1], 100) == 100


def test_unknown_engine_kind_falls_back_to_the_video_table():
    assert fold_phase("nonesuch", "denoise", 50) == fold_phase("video", "denoise", 50)


# ── elapsed helper ──────────────────────────────────────────────────────────


def test_elapsed_seconds_handles_naive_timestamps_from_sqlite():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 8, 11, 11, 58)  # what SQLite hands back
    assert elapsed_seconds(naive, now) == 120
    assert elapsed_seconds(now - timedelta(seconds=30), now) == 30
    assert elapsed_seconds(None, now) is None


def test_elapsed_seconds_never_goes_negative_on_clock_skew():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    assert elapsed_seconds(now + timedelta(seconds=5), now) == 0.0
