"""add runtime_configs table

Persists the config overrides made through ``PUT /v2/config`` (the Storage
Settings page). Before this table those edits only ever reached
``setattr`` on the in-memory ``Config`` object, so any container rebuild or
image upgrade silently reverted them to whatever the startup arguments said —
which is how a fully-tuned admission configuration could vanish with no warning.

Only explicitly-changed fields get a row; the table is an override layer, not a
snapshot of the whole config.

NOTE: like e1f2a3b4c5d6 and f3a1b2c4d5e7, the ACR overlay build
(pack/Dockerfile.acr) rewrites the CHAIN ROOT's ``down_revision`` at image-build
time to the base image's real alembic head. THIS revision stays chained to
f3a1b2c4d5e7, and the build's head assertion must name this revision.

Revision ID: a7c3e5d19b42
Revises: f3a1b2c4d5e7
Create Date: 2026-09-02 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

from gpustack.schemas.common import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = 'a7c3e5d19b42'
down_revision: Union[str, None] = 'f3a1b2c4d5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'runtime_configs'


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        # Unique-indexed column rather than the primary key: MySQL cannot index
        # an unbounded TEXT primary key, and the surrogate id keeps this table
        # shaped like the rest of the schema.
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        # JSON so dict-valued fields (lightx2v_model_latency_seconds and
        # friends) round-trip without string escaping.
        sa.Column('value', sa.JSON(), nullable=True),
        # The three TimestampsMixin columns. ``deleted_at`` is not optional
        # here even though nothing soft-deletes these rows: the ORM maps it, so
        # every ``select(RuntimeConfig)`` names it, and a table without the
        # column fails with "no such column: runtime_configs.deleted_at". That
        # breaks precisely the deployment path this feature targets — an
        # existing database upgraded through alembic — while a fresh install
        # via init_db's create_all works, because create_all builds the table
        # from the model. Nullability matches TimestampsMixin.
        sa.Column('created_at', UTCDateTime(), nullable=False),
        sa.Column('updated_at', UTCDateTime(), nullable=False),
        sa.Column('deleted_at', UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_runtime_configs_key'), TABLE, ['key'], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_runtime_configs_key'), table_name=TABLE)
    op.drop_table(TABLE)
