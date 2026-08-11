"""add progress/phase/run_started_at to video_generation_tasks

Progress reporting for async generation jobs (see
docs/视频任务进度上报-统一契约设计.md). The facade folds whatever the engine
reports into a global 0-100 and persists it: the fold is monotonic (it needs the
previous value) and ops wants a running job's stage visible without tailing the
engine.

``run_started_at`` is the elapsed-time baseline for the estimate used when an
engine reports nothing. It has to be its own column: ``updated_at`` is rewritten
by every status poll, and ``created_at`` includes the queue wait.

Columns are added with a server_default so SQLite accepts the NOT NULL on an
existing table; the ORM supplies the value on every insert regardless.

NOTE: like e1f2a3b4c5d6, the ACR overlay build (pack/Dockerfile.acr) rewrites
that migration's ``down_revision`` at image-build time to the base image's real
alembic head. THIS revision stays chained to e1f2a3b4c5d6 (the chain root is
rewritten, not the links after it), and the build's head assertion must name
this revision.

Revision ID: f3a1b2c4d5e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-11 10:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

from gpustack.schemas.common import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = 'f3a1b2c4d5e7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'video_generation_tasks'


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            'progress', sa.Float(), nullable=False, server_default=sa.text('0')
        ),
    )
    op.add_column(
        TABLE,
        sa.Column('phase', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(TABLE, sa.Column('run_started_at', UTCDateTime(), nullable=True))
    # Backfill already-finished jobs. Without this every historical row keeps the
    # server_default 0 and the management list (VideoTaskPublic serializes these
    # columns raw) renders a screen of "done · 0%". The state column persists the
    # enum MEMBER name, not its lower-case value — see list_video_tasks.
    op.execute(f"UPDATE {TABLE} SET progress = 100 WHERE state = 'DONE'")


def downgrade() -> None:
    op.drop_column(TABLE, 'run_started_at')
    op.drop_column(TABLE, 'phase')
    op.drop_column(TABLE, 'progress')
