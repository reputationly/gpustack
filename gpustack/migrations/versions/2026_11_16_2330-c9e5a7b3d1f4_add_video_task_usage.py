"""add usage to video_generation_tasks

Persists the engine's billable usage record (token / pixel counts in the vendor
compatible shape produced by vllm-omni's ``vllm_omni.metrics.usage``) so the gateway
can price a finished task.

Why a column instead of a cache: billing polls arrive after completion, sometimes long
after the engine task is reaped, and a lost record silently under-bills. GPUStack stores
the payload verbatim and never interprets it — the tariff lives in the gateway.

Nullable with no default: NULL means "this engine/model reported no usage" (older
engines, or a task that failed before generation), which the gateway must treat as
"cannot bill", not as zero.

Revision ID: c9e5a7b3d1f4
Revises: b8d4f6a2c1e9
Create Date: 2026-11-16 23:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c9e5a7b3d1f4'
down_revision: Union[str, None] = 'b8d4f6a2c1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'video_generation_tasks'


def upgrade() -> None:
    op.add_column(TABLE, sa.Column('usage', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column(TABLE, 'usage')
