"""add assigned_at to video_generation_tasks

Records when an attempt entered its instance's engine queue, so the queue
report can order the tasks waiting on one instance.

Why not an existing column: ``updated_at`` is rewritten by every status poll,
and ``created_at`` is wrong in exactly the case the report must get right — a
requeued task keeps its original creation time while rejoining the engine queue
at the BACK, so ordering by created_at would show it ahead of tasks that are
really in front of it.

Nullable with no server_default: a NULL means "no live attempt" (QUEUED, or
terminal), which is the same thing ``ATTEMPT_RESET`` writes. The backfill below
only touches rows that ARE in flight at upgrade time — without it, every task
already sitting in an engine queue would report an unknown position until it
finished.

NOTE: like e1f2a3b4c5d6, f3a1b2c4d5e7 and a7c3e5d19b42, the ACR overlay build
(pack/Dockerfile.acr) rewrites the CHAIN ROOT's ``down_revision`` at image-build
time to the base image's real alembic head. THIS revision stays chained to
a7c3e5d19b42, and the build's head assertion must name this revision.

Revision ID: b8d4f6a2c1e9
Revises: a7c3e5d19b42
Create Date: 2026-09-02 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from gpustack.schemas.common import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = 'b8d4f6a2c1e9'
down_revision: Union[str, None] = 'a7c3e5d19b42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'video_generation_tasks'


def upgrade() -> None:
    op.add_column(TABLE, sa.Column('assigned_at', UTCDateTime(), nullable=True))
    # Seed the rows that are in an engine queue right now. created_at is the
    # correct seed here and only here: these rows predate the column, so their
    # dispatch order is the only order we can still reconstruct, and it is right
    # for every row that was never requeued. The state column persists the enum
    # MEMBER name, not its lower-case value — see list_video_tasks.
    op.execute(
        f"UPDATE {TABLE} SET assigned_at = created_at "
        "WHERE state IN ('ASSIGNED', 'RUNNING')"
    )


def downgrade() -> None:
    op.drop_column(TABLE, 'assigned_at')
