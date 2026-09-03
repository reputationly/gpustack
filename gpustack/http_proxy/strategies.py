from abc import ABC, abstractmethod
import logging
from typing import Dict, List, Optional, Tuple
import itertools

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.schemas.models import ModelInstance

logger = logging.getLogger(__name__)

# Rotating tie-break cursor for ``select_least_pending_instance`` (see its
# docstring for why ties must NOT be broken randomly). Keyed by the tuple of
# tied instance ids so a fleet whose composition changes gets its own rotation
# rather than inheriting an offset that no longer means anything.
#
# Grows by one entry per distinct tied-set; bounded by the subsets of a single
# model's instance list, which is a handful of rows in practice. Deliberately
# not evicted: an entry is 2 small ints, and dropping one would only restart
# that set's rotation at 0.
_tie_break_cursor: Dict[Tuple[int, ...], int] = {}


class LoadBalancingStrategy(ABC):

    @abstractmethod
    async def select_instance(self, instances: List[ModelInstance]) -> ModelInstance:
        pass


class RoundRobinStrategy(LoadBalancingStrategy):
    def __init__(self):
        self._iterators: Dict[int, itertools.cycle] = {}
        self._instance_lists: Dict[int, List[ModelInstance]] = {}

    async def select_instance(self, instances: List[ModelInstance]) -> ModelInstance:
        if len(instances) == 0:
            raise Exception("No instances available")
        model_id = instances[0].model_id
        if (
            model_id not in self._iterators
            or self._instance_lists[model_id] != instances
        ):
            logger.debug(f"Creating new iterator for model {model_id}")
            self._iterators[model_id] = itertools.cycle(instances)
            self._instance_lists[model_id] = instances

        return next(self._iterators[model_id])


async def select_least_pending_instance(
    session: AsyncSession, instances: List[ModelInstance]
) -> Optional[ModelInstance]:
    """
    Pick the RUNNING instance carrying the fewest in-flight video tasks.

    "In-flight" == a ``video_generation_tasks`` row in ASSIGNED or RUNNING state
    mapped to that instance (see docs/lightx2v-backend-design.md §6.2). Because
    every video request is dispatched through the facade and recorded in that
    table, the per-instance in-flight count mirrors the depth of the engine's
    own in-memory FIFO queue — so "least pending" spreads load away from
    instances that are already saturated (the engine returns 503 once its queue
    of ``max_queue_size`` fills). This replaces the load-blind round-robin the
    gateway uses for stateless LLM traffic.

    Ties are broken by a process-wide rotating cursor, NOT randomly. Several
    requests routinely observe the same pre-insert counts: the count is read
    here, but the ASSIGNED row that would change it is only written much later
    (``/v1/videos`` resolves input refs and builds the engine body in between,
    in a *separate* session), so a burst of concurrent submits all see the same
    numbers. When the fleet is idle every instance ties, "least pending" carries
    no information at all, and the tie-break decides everything.

    Random tie-breaking loses that: three concurrent submits onto three idle
    instances each roll independently, so they spread perfectly only 6/27 ≈ 22%
    of the time — 67% of the time two collide onto one instance (one task
    queues behind the other while a third instance sits idle) and 11% of the
    time all three pile onto one. That was observed in production.

    The cursor makes it deterministic instead: read-modify-write of
    ``_tie_break_cursor`` contains no ``await``, so under the server's
    single-process asyncio loop (``Server.serve()``, no uvicorn ``workers``) it
    is atomic — three concurrent callers take slots 0, 1, 2 and fill the fleet.
    **This holds only while the server is single-process**; running multiple
    workers would need the reservation moved into the same transaction as the
    ASSIGNED insert (``SELECT ... FOR UPDATE``).

    Returns ``None`` only when ``instances`` is empty; the caller decides
    whether an all-busy fleet should surface as 503.
    """
    if not instances:
        return None

    # Local import: keep the video task schema out of the http_proxy import graph
    # at module load (this file is imported by the generic OpenAI proxy path).
    from gpustack.schemas.video_generation_task import (
        VideoGenerationTask,
        VideoTaskStateEnum,
    )

    instance_by_id = {inst.id: inst for inst in instances}
    # Compare against enum MEMBERS, not .value: the ORM maps ``state`` to a
    # SQLAlchemy Enum that persists the member NAME (e.g. "ASSIGNED"), so
    # filtering by the lower-case .value would match nothing. Mirrors the
    # codebase convention (e.g. ModelInstanceService.get_running_instances).
    in_flight_states = [
        VideoTaskStateEnum.ASSIGNED,
        VideoTaskStateEnum.RUNNING,
    ]

    statement = (
        select(VideoGenerationTask.instance_id, func.count())
        .where(
            col(VideoGenerationTask.instance_id).in_(list(instance_by_id.keys())),
            col(VideoGenerationTask.state).in_(in_flight_states),
        )
        .group_by(VideoGenerationTask.instance_id)
    )
    rows = (await session.exec(statement)).all()
    pending_by_instance: Dict[int, int] = {
        instance_id: count for instance_id, count in rows
    }

    # Instances with no in-flight row don't appear in the GROUP BY result → 0.
    min_pending = min(pending_by_instance.get(inst.id, 0) for inst in instances)
    # Sorted by id so the cursor key and the slot order are stable across calls;
    # without it a reordered ``instances`` list would silently reshuffle who
    # gets slot 0 and reintroduce collisions.
    least_loaded = sorted(
        (
            inst
            for inst in instances
            if pending_by_instance.get(inst.id, 0) == min_pending
        ),
        key=lambda inst: inst.id,
    )
    if len(least_loaded) == 1:
        return least_loaded[0]
    # No ``await`` between the read and the write — that is what makes this
    # atomic on the single-process event loop (see the docstring).
    key = tuple(inst.id for inst in least_loaded)
    idx = _tie_break_cursor.get(key, 0) % len(least_loaded)
    _tie_break_cursor[key] = idx + 1
    return least_loaded[idx]


class LeastPendingStrategy(LoadBalancingStrategy):
    """
    Load-aware strategy for the LightX2V video facade: routes to the RUNNING
    instance with the fewest in-flight tasks (``select_least_pending_instance``).

    Opens its own short-lived session so it satisfies the load-blind
    ``LoadBalancingStrategy`` ABC. Callers that already hold a session (the
    ``/v1/videos`` facade, the death-requeue sweeper) should call
    ``select_least_pending_instance`` directly to avoid nesting sessions.
    """

    async def select_instance(self, instances: List[ModelInstance]) -> ModelInstance:
        if not instances:
            raise Exception("No instances available")
        from gpustack.server.db import async_session

        async with async_session() as session:
            return await select_least_pending_instance(session, instances)
