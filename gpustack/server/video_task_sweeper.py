import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set

import aiohttp
from sqlmodel import col, select

from gpustack.routes.videos import fetch_engine_status_updates, redispatch_task
from gpustack.schemas.models import ModelInstance, ModelInstanceStateEnum
from gpustack.schemas.video_generation_task import (
    VideoGenerationTask,
    VideoTaskStateEnum,
)
from gpustack.server.db import async_session
from gpustack.server.services import ModelInstanceService, WorkerService

logger = logging.getLogger(__name__)

# Non-terminal states the sweeper reconciles. Enum MEMBERS, not .value: the ORM
# maps state to a SQLAlchemy Enum persisting the member NAME ("ASSIGNED"), so a
# .value ("assigned") filter would match nothing.
_IN_FLIGHT_STATES = [
    VideoTaskStateEnum.ASSIGNED,
    VideoTaskStateEnum.RUNNING,
]

# Consecutive sweeps an instance must be absent from the RUNNING set before its
# tasks are requeued. Instances transiently flap RUNNING → UNREACHABLE →
# RUNNING on a worker heartbeat blip while the engine keeps generating;
# requeuing on the first miss would double-dispatch the task.
_REQUEUE_MISS_THRESHOLD = 3

# An ASSIGNED row without a native_task_id means the facade crashed between the
# pre-submit insert and recording the engine's id. The submit timeout is 30s,
# so anything older than this never completed its dispatch.
_LOST_DISPATCH_SECONDS = 120

# In-flight tasks whose row hasn't moved for this long get a server-side engine
# status poll. GET /v1/videos/{id} normally drives progress (and bumps
# updated_at); this is the fallback for clients that stopped polling, so
# finished-but-abandoned tasks still reach a terminal state instead of
# inflating least-pending counts and pinning janitor-protected day dirs forever.
_STALE_POLL_SECONDS = 600

# Hard deadline: any task still non-terminal after this long is failed. Last
# line of defense against rows nothing else can ever resolve.
_TASK_MAX_AGE_HOURS = 24


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class VideoTaskSweeper:
    """The single background loop behind the LightX2V async facade (§6.0).

    Per sweep, in order:

    0. **Age deadline** — non-terminal tasks older than ``_TASK_MAX_AGE_HOURS``
       are failed with error_type="timeout".
    1. **Death-requeue** — LightX2V task ids live only in an instance's
       in-memory FIFO, so when an instance dies its in-flight/assigned tasks
       vanish. Any ASSIGNED/RUNNING task whose mapped instance has been out of
       the RUNNING set for ``_REQUEUE_MISS_THRESHOLD`` consecutive sweeps is
       flipped back to QUEUED (affinity cleared). ASSIGNED rows that never got
       a native id (facade crashed mid-submit) are requeued after
       ``_LOST_DISPATCH_SECONDS``.
    2. **Stale-task reconciliation** — in-flight tasks on a live instance whose
       row hasn't moved for ``_STALE_POLL_SECONDS`` get a server-side engine
       status poll (the fallback when the client stopped polling).
    3. **Re-dispatch** — QUEUED tasks are pushed to the least-pending RUNNING
       instance via the same path the facade uses, with a per-attempt retry cap
       enforced by ``redispatch_task``.

    Leader-only (started from ``_start_leader_tasks``): a single writer avoids
    two servers double-dispatching the same QUEUED task.
    """

    def __init__(
        self,
        http_client_getter: Callable[[], Optional[aiohttp.ClientSession]],
        http_client_no_proxy_getter: Callable[[], Optional[aiohttp.ClientSession]],
        interval: int = 5,
    ):
        self._interval = interval
        self._http_client_getter = http_client_getter
        self._http_client_no_proxy_getter = http_client_no_proxy_getter
        # instance_id -> consecutive sweeps it was absent from the RUNNING set.
        self._instance_miss_counts: Dict[Optional[int], int] = {}

    async def start(self):
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._sweep_once()
            except Exception as e:
                logger.error(f"Video task sweep failed: {e}", exc_info=True)

    async def _sweep_once(self):
        proxy_client = self._http_client_getter()
        no_proxy_client = self._http_client_no_proxy_getter()
        if proxy_client is None and no_proxy_client is None:
            return

        async with async_session() as session:
            in_flight = await self._get_non_terminal_tasks(session)
            queued = await self._get_queued_tasks(session)
            if not in_flight and not queued:
                self._instance_miss_counts.clear()
                return

            now = datetime.now(timezone.utc)

            # 0. Hard age deadline.
            await self._fail_aged_tasks(session, [*in_flight, *queued], now)

            # 1. Requeue tasks whose instance died (with flap grace) and tasks
            #    whose dispatch never completed.
            running_ids = await self._get_running_instance_ids(session)
            self._update_miss_counts(in_flight, running_ids)
            await self._requeue_lost_tasks(session, in_flight, running_ids, now)

            # 2. Server-side reconciliation for stale in-flight tasks on live
            #    instances (client stopped polling).
            await self._reconcile_stale_tasks(
                session, in_flight, running_ids, now, proxy_client, no_proxy_client
            )

            # 3. Re-dispatch everything currently QUEUED. Re-read so freshly
            #    requeued rows (steps 1-2) and any that found no free instance a
            #    previous sweep are all covered — a sweep with only QUEUED work
            #    (all capacity was busy last time) must still make progress.
            queued = await self._get_queued_tasks(session)
            for task in queued:
                await redispatch_task(session, proxy_client, no_proxy_client, task)

    async def _fail_aged_tasks(
        self, session, tasks: List[VideoGenerationTask], now: datetime
    ):
        for task in tasks:
            created = _aware(task.created_at)
            if created and now - created > timedelta(hours=_TASK_MAX_AGE_HOURS):
                await task.update(
                    session,
                    {
                        "state": VideoTaskStateEnum.FAILED,
                        "state_message": (
                            f"task exceeded max age ({_TASK_MAX_AGE_HOURS}h)"
                        ),
                        "error_type": "timeout",
                    },
                )
                logger.warning(f"Video task {task.task_id} timed out; failed")

    async def _requeue_lost_tasks(
        self,
        session,
        in_flight: List[VideoGenerationTask],
        running_ids: Set[int],
        now: datetime,
    ):
        for task in in_flight:
            if task.state not in _IN_FLIGHT_STATES:
                continue  # aged out above
            if task.instance_id not in running_ids:
                misses = self._instance_miss_counts.get(task.instance_id, 0)
                if misses < _REQUEUE_MISS_THRESHOLD:
                    continue
                await self._requeue(
                    session, task, "instance died; requeued", "no longer running"
                )
            elif task.state == VideoTaskStateEnum.ASSIGNED and not task.native_task_id:
                updated = _aware(task.updated_at)
                if updated and now - updated > timedelta(
                    seconds=_LOST_DISPATCH_SECONDS
                ):
                    await self._requeue(
                        session,
                        task,
                        "dispatch never completed; requeued",
                        "lost its dispatch",
                    )

    async def _reconcile_stale_tasks(
        self,
        session,
        in_flight: List[VideoGenerationTask],
        running_ids: Set[int],
        now: datetime,
        proxy_client,
        no_proxy_client,
    ):
        for task in in_flight:
            if (
                task.state not in _IN_FLIGHT_STATES
                or not task.native_task_id
                or task.instance_id not in running_ids
            ):
                continue
            updated = _aware(task.updated_at)
            if not updated or now - updated < timedelta(seconds=_STALE_POLL_SECONDS):
                continue
            instance = await ModelInstanceService(session).get_by_id(task.instance_id)
            if not instance or instance.state != ModelInstanceStateEnum.RUNNING:
                continue
            worker = await WorkerService(session).get_by_id(instance.worker_id)
            if not worker:
                continue
            updates = await fetch_engine_status_updates(
                proxy_client, no_proxy_client, worker, instance, task
            )
            if updates:
                await task.update(session, updates)
                logger.info(
                    f"Reconciled stale task {task.task_id} (state -> {task.state})"
                )

    def _update_miss_counts(
        self, in_flight: List[VideoGenerationTask], running_ids: Set[int]
    ):
        missing = {
            t.instance_id
            for t in in_flight
            if t.state in _IN_FLIGHT_STATES and t.instance_id not in running_ids
        }
        for instance_id in list(self._instance_miss_counts):
            if instance_id not in missing:
                del self._instance_miss_counts[instance_id]
        for instance_id in missing:
            self._instance_miss_counts[instance_id] = (
                self._instance_miss_counts.get(instance_id, 0) + 1
            )

    async def _requeue(
        self,
        session,
        task: VideoGenerationTask,
        state_message: str,
        log_reason: str,
    ):
        # Capture before update() setattrs instance_id to None.
        dead_instance_id = task.instance_id
        await task.update(
            session,
            {
                "state": VideoTaskStateEnum.QUEUED,
                "instance_id": None,
                "native_task_id": None,
                "state_message": state_message,
            },
        )
        logger.info(
            f"Requeued task {task.task_id} (instance {dead_instance_id} {log_reason})"
        )

    async def _get_non_terminal_tasks(self, session) -> List[VideoGenerationTask]:
        statement = select(VideoGenerationTask).where(
            col(VideoGenerationTask.state).in_(_IN_FLIGHT_STATES)
        )
        return list((await session.exec(statement)).all())

    async def _get_queued_tasks(self, session) -> List[VideoGenerationTask]:
        statement = select(VideoGenerationTask).where(
            col(VideoGenerationTask.state) == VideoTaskStateEnum.QUEUED
        )
        return list((await session.exec(statement)).all())

    async def _get_running_instance_ids(self, session) -> Set[int]:
        statement = select(ModelInstance.id).where(
            col(ModelInstance.state) == ModelInstanceStateEnum.RUNNING
        )
        return set((await session.exec(statement)).all())
