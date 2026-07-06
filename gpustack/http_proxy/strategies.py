from abc import ABC, abstractmethod
import logging
import random
from typing import Dict, List, Optional
import itertools

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from gpustack.schemas.models import ModelInstance

logger = logging.getLogger(__name__)


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

    Ties are broken randomly to avoid a thundering herd onto the lowest-id
    instance while several requests observe the same pre-insert counts. Returns
    ``None`` only when ``instances`` is empty; the caller decides whether an
    all-busy fleet should surface as 503.
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
    least_loaded = [
        inst for inst in instances if pending_by_instance.get(inst.id, 0) == min_pending
    ]
    return random.choice(least_loaded)


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
