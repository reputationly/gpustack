"""Queue position / ETA reported alongside a video task's status.

What the report has to get right, and why each is silent when broken:

- **Per-instance, not fleet-wide.** A task sits in ONE instance's engine FIFO;
  the other instances' backlogs do not block it. Counting fleet-wide would tell
  the 8th submitter to a 8-instance model "7 ahead of you" when nobody is in
  front of them — a number that looks plausible and is simply wrong.
- **Ordered by ``assigned_at``, not ``created_at``.** A requeued task keeps its
  original creation time but rejoins the engine queue at the BACK. This is the
  only case where the two clocks disagree, it is invisible in normal operation,
  and it is the entire reason the column exists.
- **Silence over guessing.** ``None`` means "cannot say"; the client falls back
  to a plain "queued". A wrong position is worse than no position.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

import gpustack
from gpustack.routes.videos import _queue_info
from gpustack.schemas.video_generation_task import (
    VideoGenerationTask,
    VideoTaskStateEnum,
)
from gpustack.server.video_progress import ATTEMPT_RESET

pytestmark = pytest.mark.asyncio

MIGRATIONS = os.path.join(os.path.dirname(gpustack.__file__), "migrations")
# Last upstream revision; everything after it is this fork's and replays on
# SQLite. See test_runtime_config_persistence for the full reasoning.
PREVIOUS_REVISION = "c4d7e8f9a0b1"

MODEL_ID = 1
MODEL_NAME = "ltx2.5-hd"
LATENCY = 265  # seconds/generation, as configured for this model in production
T0 = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def session(tmp_path, config):
    """A database built by the real migrations, not by ``create_all``.

    The ORM path cannot disagree with the model, so it could never catch a
    column the migration forgot — which is exactly the bug that shipped last
    time this table gained columns.
    """
    db_path = tmp_path / "queue.db"
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", MIGRATIONS)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.stamp(cfg, PREVIOUS_REVISION)
    alembic_command.upgrade(cfg, "head")

    # _queue_info reads the latency table off the global config.
    config.lightx2v_model_latency_seconds = {MODEL_NAME: LATENCY}

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


async def _task(
    session,
    task_id: str,
    state: VideoTaskStateEnum,
    *,
    instance_id=None,
    assigned_at=None,
    created_at=T0,
) -> VideoGenerationTask:
    return await VideoGenerationTask.create(
        session,
        VideoGenerationTask(
            task_id=task_id,
            model_id=MODEL_ID,
            model_name=MODEL_NAME,
            task_type="t2v",
            state=state,
            instance_id=instance_id,
            assigned_at=assigned_at,
            created_at=created_at,
        ),
    )


# --------------------------------------------------------------- schema


async def test_migration_creates_every_column_the_orm_maps(session):
    """One missing column turns every ``select(VideoGenerationTask)`` into
    ``no such column`` — on upgraded databases only, which is the deployment
    path that matters."""
    from sqlalchemy import inspect as sa_inspect

    def _columns(sync_conn):
        return {
            c["name"]
            for c in sa_inspect(sync_conn).get_columns("video_generation_tasks")
        }

    migrated = await session.run_sync(lambda s: _columns(s.connection()))
    mapped = set(VideoGenerationTask.__table__.columns.keys())
    assert (
        mapped <= migrated
    ), f"migration is missing column(s) the ORM maps: {sorted(mapped - migrated)}"
    assert "assigned_at" in migrated


# ------------------------------------------------------------- position


async def test_only_tasks_on_my_own_instance_are_ahead_of_me(session):
    """The other instances' backlogs drain in parallel and never block me.

    Fleet-wide counting would report 3 here instead of 1, which is the number a
    user would read as "this queue is hopeless" on an idle-enough fleet.
    """
    await _task(
        session,
        "mine-inst1-earlier",
        VideoTaskStateEnum.RUNNING,
        instance_id=1,
        assigned_at=T0,
    )
    for i, tid in enumerate(["other-a", "other-b"]):
        await _task(
            session,
            tid,
            VideoTaskStateEnum.ASSIGNED,
            instance_id=2,
            assigned_at=T0 + timedelta(seconds=i),
        )
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.ASSIGNED,
        instance_id=1,
        assigned_at=T0 + timedelta(seconds=10),
    )

    info = await _queue_info(mine, session)
    assert info["queue_ahead"] == 1
    assert info["estimated_start_seconds"] == LATENCY


async def test_requeued_task_is_ordered_by_assigned_at_not_created_at(session):
    """The case the ``assigned_at`` column exists for.

    ``requeued`` was submitted first, so its ``created_at`` is the oldest. Its
    instance then died, it went back to QUEUED and was re-dispatched — landing
    at the BACK of the engine's FIFO, behind ``mine``. Ordering by created_at
    would report it as ahead of us and inflate every position on this instance
    by one, for as long as the requeued task runs.
    """
    requeued = await _task(
        session,
        "requeued",
        VideoTaskStateEnum.ASSIGNED,
        instance_id=1,
        created_at=T0,  # submitted first...
        assigned_at=T0 + timedelta(minutes=30),  # ...but re-dispatched last
    )
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.ASSIGNED,
        instance_id=1,
        created_at=T0 + timedelta(minutes=5),
        assigned_at=T0 + timedelta(minutes=5),
    )

    assert (await _queue_info(mine, session))[
        "queue_ahead"
    ] == 0, "a task that rejoined the queue behind me was counted in front of me"
    # And the mirror image: we really are in front of it.
    assert (await _queue_info(requeued, session))["queue_ahead"] == 1


async def test_finished_neighbours_do_not_hold_a_place(session):
    """Only ASSIGNED/RUNNING rows occupy the engine's queue."""
    for state in (
        VideoTaskStateEnum.DONE,
        VideoTaskStateEnum.FAILED,
        VideoTaskStateEnum.CANCELED,
    ):
        await _task(
            session,
            f"old-{state.value}",
            state,
            instance_id=1,
            assigned_at=T0,
        )
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.ASSIGNED,
        instance_id=1,
        assigned_at=T0 + timedelta(seconds=10),
    )
    assert (await _queue_info(mine, session))["queue_ahead"] == 0


async def test_running_task_reports_zero_not_unknown(session):
    """A running task is not waiting; the client should render 0, not fall back
    to a bare "queued"."""
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.RUNNING,
        instance_id=1,
        assigned_at=T0,
    )
    info = await _queue_info(mine, session)
    assert info == {"queue_ahead": 0, "estimated_start_seconds": 0}


async def test_terminal_task_reports_no_queue(session):
    """A finished job has no position. Reporting 0 would render as "starting
    now" next to status=done."""
    mine = await _task(session, "mine", VideoTaskStateEnum.DONE, instance_id=1)
    info = await _queue_info(mine, session)
    assert info == {"queue_ahead": None, "estimated_start_seconds": None}


async def test_assigned_without_assigned_at_says_unknown(session):
    """A row mid-dispatch (or one the backfill missed) has no place to report.
    Guessing 0 would promise an immediate start."""
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.ASSIGNED,
        instance_id=1,
        assigned_at=None,
    )
    assert (await _queue_info(mine, session))["queue_ahead"] is None


# ---------------------------------------------------------------- queued


def _fleet(monkeypatch, count: int):
    """Pin the number of RUNNING instances the QUEUED branch divides by."""
    from gpustack.routes import videos as videos_module

    class _Svc:
        def __init__(self, _session):
            pass

        async def get_running_instances(self, _model_id):
            return [object()] * count

    monkeypatch.setattr(videos_module, "ModelInstanceService", _Svc)


async def test_queued_task_divides_the_backlog_by_instance_count(session, monkeypatch):
    """With no instance yet, fall back to the fleet-wide estimate — the same
    ``depth // instances`` arithmetic admission control used to let this task
    in, so the two can never contradict each other."""
    _fleet(monkeypatch, 4)

    for i in range(8):
        await _task(
            session,
            f"ahead-{i}",
            VideoTaskStateEnum.ASSIGNED,
            instance_id=(i % 4) + 1,
            created_at=T0 + timedelta(seconds=i),
            assigned_at=T0 + timedelta(seconds=i),
        )
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.QUEUED,
        created_at=T0 + timedelta(minutes=1),
    )

    info = await _queue_info(mine, session)
    assert info["queue_ahead"] == 2  # 8 ahead over 4 instances
    assert info["estimated_start_seconds"] == 2 * LATENCY


async def test_queued_behind_a_busy_fleet_never_says_starting_now(session, monkeypatch):
    """The case that makes the QUEUED branch worth having — and the one it used
    to get exactly backwards.

    A QUEUED row is ALWAYS a requeued one: the insert path is born ASSIGNED, so
    only the sweeper's requeue and the engine-404 fold produce this state. Its
    created_at is therefore the ORIGINAL submission — older than the tasks that
    took the instances while it was dead. Counting only rows created before it
    finds nothing, divides to 0, and tells a task queued behind a fully busy
    fleet that it starts now. The work blocking it holds engine slots
    regardless of when it was submitted.
    """
    _fleet(monkeypatch, 4)

    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.QUEUED,
        created_at=T0,  # submitted long before everything below...
    )
    for i in range(4):  # ...which took every instance while we were dead
        await _task(
            session,
            f"took-my-slot-{i}",
            VideoTaskStateEnum.RUNNING if i else VideoTaskStateEnum.ASSIGNED,
            instance_id=i + 1,
            created_at=T0 + timedelta(minutes=20 + i),
            assigned_at=T0 + timedelta(minutes=20 + i),
        )

    info = await _queue_info(mine, session)
    assert info["queue_ahead"] == 1, "a full fleet was reported as immediate capacity"
    assert info["estimated_start_seconds"] == LATENCY


async def test_queued_peers_are_split_by_created_at(session, monkeypatch):
    """Among rows that are all waiting, the older ones go first. The sweeper
    drains QUEUED with no ORDER BY, so this is the fairest split available
    rather than a promise — but a task must not be told it is at the front of a
    queue it is at the back of."""
    _fleet(monkeypatch, 2)

    for i in range(4):
        await _task(
            session,
            f"waiting-longer-{i}",
            VideoTaskStateEnum.QUEUED,
            created_at=T0 + timedelta(seconds=i),
        )
    for i in range(3):
        await _task(
            session,
            f"waiting-less-{i}",
            VideoTaskStateEnum.QUEUED,
            created_at=T0 + timedelta(minutes=10 + i),
        )
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.QUEUED,
        created_at=T0 + timedelta(minutes=5),
    )

    assert (await _queue_info(mine, session))["queue_ahead"] == 2  # 4 over 2


async def test_queued_on_an_idle_fleet_starts_now(session, monkeypatch):
    """0 is a positive statement ("nothing has to finish first"), and here it is
    true: no in-flight work, no older peers."""
    _fleet(monkeypatch, 2)
    mine = await _task(session, "mine", VideoTaskStateEnum.QUEUED)
    info = await _queue_info(mine, session)
    assert info == {"queue_ahead": 0, "estimated_start_seconds": 0}


async def test_queued_ignores_finished_neighbours(session, monkeypatch):
    """Terminal rows hold no capacity; counting them would inflate every
    position on a model for as long as its history is retained."""
    _fleet(monkeypatch, 2)
    for state in (
        VideoTaskStateEnum.DONE,
        VideoTaskStateEnum.FAILED,
        VideoTaskStateEnum.CANCELED,
    ):
        await _task(
            session,
            f"old-{state.value}",
            state,
            instance_id=1,
            created_at=T0,
            assigned_at=T0,
        )
    mine = await _task(
        session,
        "mine",
        VideoTaskStateEnum.QUEUED,
        created_at=T0 + timedelta(minutes=5),
    )
    assert (await _queue_info(mine, session))["queue_ahead"] == 0


async def test_queued_with_no_running_instance_says_unknown(session, monkeypatch):
    """Nothing is draining the queue, so any ETA would be fiction."""
    _fleet(monkeypatch, 0)
    mine = await _task(session, "mine", VideoTaskStateEnum.QUEUED)
    assert (await _queue_info(mine, session))["queue_ahead"] is None


# ------------------------------------------------------------- contract


async def test_attempt_reset_clears_the_queue_slot():
    """Every caller that ENDS an attempt folds in ATTEMPT_RESET. A dead attempt
    holds no place in any engine queue, so leaving assigned_at behind would put
    a requeued task's stale position into the report while it waits for a fresh
    instance."""
    assert ATTEMPT_RESET["assigned_at"] is None


async def test_both_dispatch_paths_stamp_the_slot_after_the_engine_accepts():
    """The two paths must use the same instant, or the report contradicts the
    engine.

    ``create_video_task`` writes its row BEFORE submitting (so a crash cannot
    orphan the job), and that row is born ASSIGNED — so it is tempting to stamp
    assigned_at in the same constructor. That dates the attempt up to
    _SUBMIT_TIMEOUT earlier than the engine queued it, while
    ``redispatch_task`` stamps after a successful submit; two tasks racing onto
    one instance would then be reported in the opposite order to the one the
    engine runs them in. Source-level because the alternative is standing up a
    worker, an instance and a fake engine to observe one timestamp.
    """
    import inspect

    from gpustack.routes.videos import create_video_task, redispatch_task

    stamp = '"assigned_at": datetime.now(timezone.utc)'
    for fn in (create_video_task, redispatch_task):
        before, submitted, after = inspect.getsource(fn).partition("_submit_to_engine(")
        assert submitted, f"{fn.__name__} no longer submits to the engine"
        # Matches an assignment (constructor kwarg or dict entry), not the word
        # itself — the pre-submit block explains in prose why it leaves the
        # column NULL, and that comment must not trip this.
        assert (
            "assigned_at=" not in before and stamp not in before
        ), f"{fn.__name__} stamps assigned_at before the engine accepts the job"
        assert stamp in after, f"{fn.__name__} never stamps assigned_at"

    # Re-dispatch has a second, invisible ordering constraint: its stamp shares
    # an update dict with **ATTEMPT_RESET, which sets assigned_at to None. A
    # dict literal keeps the LAST value for a repeated key, so a stamp written
    # above the spread is silently nulled — the task drops out of the
    # per-instance ordering and reports unknown for the rest of its life, with
    # nothing raising anywhere. The check above cannot see this: both positions
    # are after the submit.
    src = inspect.getsource(redispatch_task)
    assert src.index("**ATTEMPT_RESET") < src.index(
        stamp
    ), "redispatch_task stamps assigned_at above the ATTEMPT_RESET spread, which nulls it"


async def test_no_row_is_ever_queued_from_birth():
    """The invariant the QUEUED branch is built on.

    That branch counts ALL in-flight rows for the model, not only those created
    before the task, because a queued row's ``created_at`` is its ORIGINAL
    submission — older than the work that took the instances while it was dead.
    The reasoning holds only while QUEUED is unreachable at insert time. Let a
    genuinely new task be inserted QUEUED and it lands on the
    created_at-filtered side of that query with nothing in front of it: the
    report answers "starting now" to a task queued behind a full fleet, which
    is precisely the bug the branch was rewritten to fix.

    Source-level because proving a state is unreachable is not something a
    fixture can do. It pins the realistic regression — someone rewriting the
    insert to "queue it and let the sweeper dispatch" — not every conceivable
    new insert path.
    """
    import inspect

    from gpustack.routes.videos import create_video_task

    src = inspect.getsource(create_video_task)
    assert (
        "state=VideoTaskStateEnum.ASSIGNED" in src
    ), "the insert path no longer writes ASSIGNED; _queue_counts' QUEUED branch assumes it does"
    assert (
        "state=VideoTaskStateEnum.QUEUED" not in src
    ), "a task inserted QUEUED would be reported as 'starting now' behind a full fleet"
