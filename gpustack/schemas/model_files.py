from datetime import datetime
from enum import Enum
from typing import ClassVar, List, Optional, Union
from sqlmodel import (
    JSON,
    BigInteger,
    Column,
    Field,
    ForeignKey,
    Integer,
    Relationship,
    SQLModel,
    Text,
)

from gpustack.mixins import BaseModelMixin
from gpustack.schemas.common import ListParams, PaginatedList
from gpustack.schemas.links import (
    ModelInstanceDraftModelFileLink,
    ModelInstanceModelFileLink,
)
from gpustack.schemas.models import ModelInstance, ModelSource, SourceEnum


class ModelFileStateEnum(str, Enum):
    ERROR = "error"
    DOWNLOADING = "downloading"
    READY = "ready"


class ModelFileBase(SQLModel, ModelSource):
    local_dir: Optional[str] = None
    worker_id: Optional[int] = None
    cleanup_on_delete: Optional[bool] = None

    is_lora: bool = Field(default=False, nullable=False)
    base_model: Optional[str] = Field(default=None, nullable=True)

    size: Optional[int] = Field(sa_column=Column(BigInteger), default=None)
    download_progress: Optional[float] = None
    resolved_paths: List[str] = Field(sa_column=Column(JSON), default=[])
    state: ModelFileStateEnum = ModelFileStateEnum.DOWNLOADING
    state_message: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )


class ModelFile(ModelFileBase, BaseModelMixin, table=True):
    __tablename__ = 'model_files'
    id: Optional[int] = Field(default=None, primary_key=True)

    # Unique index of the model source
    source_index: Optional[str] = Field(index=True, unique=True, default=None)

    # Tenant scope. Server-derived from worker→cluster on creation; not
    # exposed on the create payload to avoid clients smuggling overrides.
    cluster_id: Optional[int] = Field(default=None)
    owner_principal_id: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("principals.id"), nullable=True),
    )

    instances: list[ModelInstance] = Relationship(
        sa_relationship_kwargs={"lazy": "noload"},
        back_populates="model_files",
        link_model=ModelInstanceModelFileLink,
    )

    draft_instances: list[ModelInstance] = Relationship(
        back_populates="draft_model_files",
        link_model=ModelInstanceDraftModelFileLink,
        sa_relationship_kwargs={"lazy": "noload"},
    )


class ModelFileListParams(ListParams):
    sortable_fields: ClassVar[List[str]] = [
        "source",
        "worker_id",
        "state",
        "resolved_paths",
        "created_at",
        "updated_at",
    ]


class ModelFileCreate(ModelFileBase):
    pass


class ModelFileUpdate(ModelFileBase):
    pass


class ModelFilePublic(SQLModel):
    """Read-side view of a model file — **no rejecting validation**.

    It used to inherit ``ModelFileBase`` → ``ModelSource``, whose
    ``check_huggingface_fields`` rejects a row whose ``source`` disagrees with the
    populated id/path column. A download row left in that state by a hand-edit made
    the whole model-files list answer 500. Same defect class as
    ``ModelProviderPublic`` rejecting zero API tokens.

    Field list duplicated on purpose; ``tests/routers/test_model_file_public_isolation.py``
    pins the field set against the table columns.
    """

    id: int
    # The owning Org, denormalized from worker → cluster on create.
    # Lives on the row but is intentionally absent from the public payloads of
    # Create / Update since clients must not smuggle their own tenant override.
    # Surfaced here so list / get responses can render which Org owns the file.
    owner_principal_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    source: Union[SourceEnum, str]
    huggingface_repo_id: Optional[str] = None
    huggingface_filename: Optional[str] = None
    model_scope_model_id: Optional[str] = None
    model_scope_file_path: Optional[str] = None
    local_path: Optional[str] = None
    local_dir: Optional[str] = None
    worker_id: Optional[int] = None
    cleanup_on_delete: Optional[bool] = None
    is_lora: bool = False
    base_model: Optional[str] = None
    size: Optional[int] = None
    download_progress: Optional[float] = None
    resolved_paths: List[str] = []
    state: Union[ModelFileStateEnum, str] = ModelFileStateEnum.DOWNLOADING
    state_message: Optional[str] = None


ModelFilesPublic = PaginatedList[ModelFilePublic]
