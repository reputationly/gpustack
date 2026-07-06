"""add video_generation_tasks

Central persistent queue for the LightX2V async dispatcher (see
docs/lightx2v-backend-design.md §6.1/§6.5). One row per submitted generation
job: the public ``task_id`` (what POST /v1/videos returns and clients poll via
GET /v1/videos/{id}), the ``(instance_id, native_task_id, nfs_path)`` affinity
mapping (§6.2 — LightX2V task ids are in-memory per instance, so follow-ups must
find the original instance), and the queued/assigned/running/done/failed state
machine. Re-dispatch is driven by the leader-only VideoTaskSweeper, so no
row-level locking is needed.

Column names/types mirror gpustack/schemas/video_generation_task.py: ``state``
must be the native ``videotaskstateenum`` (the ORM maps the field to
``sa.Enum``; on postgresql+asyncpg every bind is cast to that type, so a plain
VARCHAR column would fail with "type videotaskstateenum does not exist").
Also extends ``operationenum`` with VIDEO_GENERATION so /v1/videos submissions
can be recorded by the usage middleware. Applied on server start via
``alembic upgrade``.

NOTE: ``down_revision`` below is correct for THIS repo's full migration chain.
The ACR overlay build (pack/Dockerfile.acr) rewrites it at image-build time to
the base image's actual alembic head, because the released base image does not
ship upstream-main migrations like c4d7e8f9a0b1 (KeyError at startup
otherwise, 2026-07-06 deploy).

Revision ID: e1f2a3b4c5d6
Revises: c4d7e8f9a0b1
Create Date: 2026-07-05 22:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

from gpustack.schemas.common import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'c4d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'video_generation_tasks'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),
        sa.Column('task_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('model_id', sa.Integer(), nullable=True),
        sa.Column('model_name', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('owner_user_id', sa.Integer(), nullable=True),
        sa.Column('task_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=True),
        sa.Column('params', sa.JSON(), nullable=True),
        sa.Column(
            'state',
            sa.Enum(
                'QUEUED',
                'ASSIGNED',
                'RUNNING',
                'DONE',
                'FAILED',
                'CANCELED',
                name='videotaskstateenum',
            ),
            nullable=False,
        ),
        sa.Column('state_message', sa.Text(), nullable=True),
        sa.Column('instance_id', sa.Integer(), nullable=True),
        sa.Column('native_task_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('nfs_path', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('output_root', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('error_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('updated_at', UTCDateTime(), nullable=False),
        # Inherited from TimestampsMixin (soft-delete marker). The ORM maps this
        # column, so it must exist or every select/insert on the table fails.
        sa.Column('deleted_at', UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(f'ix_{TABLE}_task_id', TABLE, ['task_id'], unique=True)
    op.create_index(f'ix_{TABLE}_model_id', TABLE, ['model_id'], unique=False)
    op.create_index(f'ix_{TABLE}_model_name', TABLE, ['model_name'], unique=False)
    op.create_index(f'ix_{TABLE}_user_id', TABLE, ['user_id'], unique=False)
    op.create_index(
        f'ix_{TABLE}_owner_user_id', TABLE, ['owner_user_id'], unique=False
    )
    op.create_index(f'ix_{TABLE}_state', TABLE, ['state'], unique=False)
    op.create_index(f'ix_{TABLE}_instance_id', TABLE, ['instance_id'], unique=False)

    # Extend operationenum so the usage middleware can record /v1/videos
    # submissions. Same guarded-ALTER pattern as 2024_12_26 (e6bf9e067296);
    # non-PG dialects store the enum as VARCHAR and need no DDL.
    conn = op.get_bind()
    if conn.dialect.name == 'postgresql':
        existing = [
            row[0]
            for row in conn.execute(
                sa.text("SELECT unnest(enum_range(NULL::operationenum))::text")
            ).fetchall()
        ]
        if 'VIDEO_GENERATION' not in existing:
            conn.execute(
                sa.text("ALTER TYPE operationenum ADD VALUE 'VIDEO_GENERATION'")
            )


def downgrade() -> None:
    op.drop_index(f'ix_{TABLE}_instance_id', table_name=TABLE)
    op.drop_index(f'ix_{TABLE}_state', table_name=TABLE)
    op.drop_index(f'ix_{TABLE}_owner_user_id', table_name=TABLE)
    op.drop_index(f'ix_{TABLE}_user_id', table_name=TABLE)
    op.drop_index(f'ix_{TABLE}_model_name', table_name=TABLE)
    op.drop_index(f'ix_{TABLE}_model_id', table_name=TABLE)
    op.drop_index(f'ix_{TABLE}_task_id', table_name=TABLE)
    op.drop_table(TABLE)
    # PG enum values can't be dropped from operationenum; the extra
    # VIDEO_GENERATION value is harmless and intentionally left in place.
    sa.Enum(name='videotaskstateenum').drop(op.get_bind(), checkfirst=True)
