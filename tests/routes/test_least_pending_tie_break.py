"""Tie-breaking in ``select_least_pending_instance``.

The load-aware part of that function is only half the story. The half that
actually decided production behaviour is what happens when every candidate
ties — because an IDLE fleet is exactly the all-tied case, and then
"least pending" carries no information whatsoever.

Observed before the fix: three concurrent submits (one playground request with
张数=3) onto three idle U1.5 instances left two tasks running and one queued
while the third instance sat idle. Random tie-breaking spreads three requests
across three instances only 6/27 ≈ 22% of the time.

Why the burst all sees the same counts: the count is read here, but the
ASSIGNED row that would change it is written much later by ``/v1/videos``, in a
separate session, after input-ref resolution. So concurrency is not incidental
to this function — it is the normal case, and the tie-break has to survive it.
"""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

import gpustack
from gpustack.http_proxy import strategies
from gpustack.http_proxy.strategies import select_least_pending_instance
from gpustack.schemas.models import ModelInstance
from gpustack.schemas.video_generation_task import (
    VideoGenerationTask,
    VideoTaskStateEnum,
)

pytestmark = pytest.mark.asyncio

MIGRATIONS = os.path.join(os.path.dirname(gpustack.__file__), "migrations")
PREVIOUS_REVISION = "c4d7e8f9a0b1"

MODEL_ID = 1
MODEL_NAME = "sensenova-u1.5"
T0 = datetime(2026, 9, 3, 15, 22, 53, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def session(tmp_path):
    db_path = tmp_path / "tie.db"
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", MIGRATIONS)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.stamp(cfg, PREVIOUS_REVISION)
    alembic_command.upgrade(cfg, "head")

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_cursor():
    """The cursor is process-wide; leaking it between tests makes slot
    assertions depend on test order — the classic way a rotation test passes
    alone and fails in a suite."""
    strategies._tie_break_cursor.clear()
    yield
    strategies._tie_break_cursor.clear()


def _instances(*ids) -> list:
    return [
        ModelInstance(id=i, model_id=MODEL_ID, model_name=MODEL_NAME, worker_id=i)
        for i in ids
    ]


async def _in_flight(session, task_id: str, instance_id: int, state):
    await VideoGenerationTask.create(
        session,
        VideoGenerationTask(
            task_id=task_id,
            model_id=MODEL_ID,
            model_name=MODEL_NAME,
            task_type="t2i",
            state=state,
            instance_id=instance_id,
            created_at=T0,
        ),
    )


async def test_idle_fleet_burst_fills_every_instance(session):
    """The production symptom, as a test.

    Three selections with NO insert in between — that is exactly what the
    facade does, since the ASSIGNED rows are written later in another session.
    Every call therefore sees three zeros and every instance ties.
    """
    instances = _instances(8, 9, 10)
    picked = [
        (await select_least_pending_instance(session, instances)).id for _ in range(3)
    ]
    assert sorted(picked) == [8, 9, 10], (
        f"three concurrent submits landed on {picked} — a repeat means one "
        f"instance queues a task while another stays idle"
    )


async def test_rotation_wraps(session):
    """Six submits onto three idle instances = two each, not a random pile."""
    instances = _instances(8, 9, 10)
    picked = [
        (await select_least_pending_instance(session, instances)).id for _ in range(6)
    ]
    assert sorted(picked) == [8, 8, 9, 9, 10, 10]


async def test_load_still_wins_over_rotation(session):
    """The rotation must only break TIES — it must never override a real
    load difference, or the fix would trade one dispatch bug for another."""
    instances = _instances(8, 9, 10)
    await _in_flight(session, "t-a", 8, VideoTaskStateEnum.RUNNING)
    await _in_flight(session, "t-b", 9, VideoTaskStateEnum.ASSIGNED)
    # 10 is the only instance with nothing in flight; it wins every time,
    # however far the cursor has advanced.
    for _ in range(4):
        assert (await select_least_pending_instance(session, instances)).id == 10


async def test_rotation_covers_only_the_tied_subset(session):
    """With one instance loaded, the other two share the rotation between
    themselves — the busy one must not get a turn just because its slot came up."""
    instances = _instances(8, 9, 10)
    await _in_flight(session, "t-a", 8, VideoTaskStateEnum.RUNNING)
    picked = [
        (await select_least_pending_instance(session, instances)).id for _ in range(4)
    ]
    assert picked == [9, 10, 9, 10]


async def test_instance_order_does_not_change_slots(session):
    """Callers get ``instances`` from a DB query with no ORDER BY. If the
    rotation keyed off list order, a reshuffled result would hand out the same
    slot twice and quietly bring the collisions back."""
    picked = [
        (await select_least_pending_instance(session, _instances(8, 9, 10))).id,
        (await select_least_pending_instance(session, _instances(10, 8, 9))).id,
        (await select_least_pending_instance(session, _instances(9, 10, 8))).id,
    ]
    assert sorted(picked) == [8, 9, 10]


async def test_single_candidate_is_returned_without_rotation(session):
    instances = _instances(8)
    for _ in range(3):
        assert (await select_least_pending_instance(session, instances)).id == 8


async def test_empty_fleet_returns_none(session):
    assert await select_least_pending_instance(session, []) is None
