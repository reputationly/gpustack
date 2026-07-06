import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import col

from gpustack.api.exceptions import BadRequestException
from gpustack.schemas.video_generation_task import (
    VideoGenerationTask,
    VideoTaskListParams,
    VideoTaskStateEnum,
    VideoTasksPublic,
)
from gpustack.server.db import async_session
from gpustack.server.deps import CurrentUserDep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=VideoTasksPublic)
async def list_video_tasks(
    user: CurrentUserDep,
    params: VideoTaskListParams = Depends(),
    search: Optional[str] = None,
    state: Optional[str] = None,
    model_id: Optional[int] = Query(default=None),
):
    """Paginated LightX2V video-job list for the admin task panel.

    Owner-scoped: a non-admin caller only sees the jobs they submitted
    (``owner_user_id``); admins see everything. This is the management view —
    the OpenAI-compatible job API stays on ``/v1/videos``.
    """
    fields: dict = {}
    if model_id is not None:
        fields["model_id"] = model_id
    if state:
        try:
            # Compare against the enum member (the column persists the member
            # name, not the lower-case value) — see select_least_pending_instance.
            fields["state"] = VideoTaskStateEnum(state)
        except ValueError:
            raise BadRequestException(message=f"Invalid state '{state}'")

    extra_conditions = []
    if not user.is_admin:
        extra_conditions.append(col(VideoGenerationTask.owner_user_id) == user.id)

    fuzzy_fields = {"model_name": search} if search else None

    async with async_session() as session:
        return await VideoGenerationTask.paginated_by_query(
            session=session,
            fields=fields or None,
            fuzzy_fields=fuzzy_fields,
            extra_conditions=extra_conditions or None,
            page=params.page,
            per_page=params.perPage,
            order_by=params.order_by,
        )
