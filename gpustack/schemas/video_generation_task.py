from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import ListParams, PaginatedList


class VideoTaskStateEnum(str, Enum):
    r"""
    Central-queue task lifecycle (see docs/lightx2v-backend-design.md §6.1).

        QUEUED --> ASSIGNED --> RUNNING --> DONE
           ^           |            |
           |           +----> FAILED (task-level failure; re-queued up to a
           |                          retry cap, or reported failed)
           +--- (instance died: in-flight/assigned tasks go back to QUEUED)

    CANCELED is terminal (client cancel).
    """

    QUEUED = "queued"  # accepted, waiting for a free instance
    ASSIGNED = "assigned"  # dispatched to an instance, native id recorded
    RUNNING = "running"  # instance is generating
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"

    def __str__(self):
        return self.value


# Terminal states — the dispatcher/janitor treat these as final.
VIDEO_TASK_TERMINAL_STATES = {
    VideoTaskStateEnum.DONE,
    VideoTaskStateEnum.FAILED,
    VideoTaskStateEnum.CANCELED,
}


class VideoTaskBase(SQLModel):
    # Public job id returned by POST /v1/videos and polled via GET
    # /v1/videos/{id}. Clients only ever use this; the (instance,
    # native_task_id) mapping below is internal (task affinity, §6.2), so a
    # caller never needs to know which instance served it.
    task_id: str = Field(index=True, unique=True)

    model_id: Optional[int] = Field(default=None, index=True)
    model_name: Optional[str] = Field(default=None, index=True)

    # user_id comes from new-api (not GPUStack's user system); default 0. Drives
    # the NFS/OBS path convention (§7.2).
    user_id: int = Field(default=0, index=True)

    # GPUStack principal (auth user) that submitted the job — distinct from
    # ``user_id`` above (new-api's end-user). Used to authorize GET/content:
    # only the submitter or an admin may read a task's status/result, so a
    # task_id learned by another tenant can't be used to read across tenants.
    owner_user_id: Optional[int] = Field(default=None, index=True)

    # LightX2V task action: t2i / i2i / t2v / i2v / flf2v / s2v (§7.2).
    task_type: str = Field(default="")

    prompt: Optional[str] = Field(default=None, sa_column=Column(Text))
    # Per-request params: seed, negative_prompt, aspect_ratio,
    # target_video_length, resize_mode, resolved input paths, etc.
    params: Optional[Dict[str, Any]] = Field(sa_column=Column(JSON), default=None)

    state: VideoTaskStateEnum = Field(default=VideoTaskStateEnum.QUEUED, index=True)
    state_message: Optional[str] = Field(default=None, sa_column=Column(Text))

    # Affinity mapping (§6.2): the model instance a task was dispatched to and
    # the engine-native task id on that instance (only valid there — LightX2V
    # task ids are in-memory per instance).
    instance_id: Optional[int] = Field(default=None, index=True)
    native_task_id: Optional[str] = Field(default=None)

    # Result location. GPUStack only writes NFS and returns nfs_path (§7.3);
    # new-api reads it and owns OBS upload / delivery URLs.
    nfs_path: Optional[str] = Field(default=None)
    # The lightx2v_output_root in effect when the task was created. nfs_path is
    # validated against THIS root (not the live config value), so editing the
    # root at runtime doesn't invalidate previously generated results.
    output_root: Optional[str] = Field(default=None)

    # Dispatch attempts after the initial one (every re-dispatch, successful or
    # transiently failed, consumes one). Capped by the sweeper's retry limit.
    retry_count: int = Field(default=0)
    error_type: Optional[str] = Field(default=None)


class VideoGenerationTask(VideoTaskBase, BaseModelMixin, table=True):
    __tablename__ = "video_generation_tasks"
    id: Optional[int] = Field(default=None, primary_key=True)


class VideoTaskCreate(VideoTaskBase):
    pass


class VideoTaskUpdate(VideoTaskBase):
    pass


class VideoTaskPublic(VideoTaskBase):
    id: int
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


VideoTasksPublic = PaginatedList[VideoTaskPublic]


class VideoTaskListParams(ListParams):
    sortable_fields: ClassVar[List[str]] = [
        "state",
        "model_name",
        "user_id",
        "task_type",
        "created_at",
        "updated_at",
    ]
