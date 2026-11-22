"""backfill models.distributed_inference_across_workers

The API used to *derive* this flag while serializing a model row
(``ModelPublic`` filled it from the backend when the column was NULL). That made
the API and the scheduler disagree for such rows: the response showed "on" for a
vLLM model, while the scheduler read the row itself and treated NULL as false —
so the UI described a placement rule the scheduler did not apply.

Derivation now lives on the write side only (``ModelSpecBase.set_defaults``), which
means a NULL can no longer be written through the API. This migration removes the
remaining NULLs — rows from fresh installs (whose tables come from ``create_all``,
where the column is nullable) or from hand edits — using exactly the rule the API
used to apply, so the values the clients already saw stay stable while the read
path becomes faithful.

Normally this touches zero rows on an upgraded database: the 2024-09-11 migration
added the column as ``NOT NULL DEFAULT 1`` there.

Revision ID: d4b8e2a16c37
Revises: c9e5a7b3d1f4
Create Date: 2026-11-22 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4b8e2a16c37'
down_revision: Union[str, None] = 'c9e5a7b3d1f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors gpustack.schemas.models: get_backend() + the derived membership test.
# Kept as literals (not app imports) so the migration stays valid against the
# schema as it is today, even if the enums grow later.
_DISTRIBUTED_BACKENDS = ('vLLM', 'MindIE', 'SGLang')
_FALLBACK_BACKEND = 'vLLM'  # get_backend()'s default when nothing else applies
_GGUF_BACKEND = 'Custom'  # get_backend() for gguf checkpoints

models = sa.table(
    'models',
    sa.column('id', sa.Integer),
    sa.column('backend', sa.String),
    sa.column('source', sa.String),
    sa.column('huggingface_filename', sa.String),
    sa.column('model_scope_file_path', sa.String),
    sa.column('local_path', sa.String),
    sa.column('distributed_inference_across_workers', sa.Boolean),
)


#: ``source`` is declared with the SQLAlchemy Enum of ``SourceEnum``, which stores
#: the member NAMES — a live database reports ``HUGGING_FACE``, not the Python
#: value ``huggingface``. Normalising both spellings keeps this rule correct on
#: PostgreSQL (enum column) and on SQLite/MySQL (where the same column may come
#: back as the name or the value depending on how it was created).
_SOURCE_ALIASES = {
    'HUGGINGFACE': 'HUGGING_FACE',
    'HUGGING_FACE': 'HUGGING_FACE',
    'MODELSCOPE': 'MODEL_SCOPE',
    'MODEL_SCOPE': 'MODEL_SCOPE',
    'LOCALPATH': 'LOCAL_PATH',
    'LOCAL_PATH': 'LOCAL_PATH',
}


def _source_name(value) -> str:
    """Canonical source kind for whatever the database hands back."""
    raw = str(value or '').strip()
    return _SOURCE_ALIASES.get(raw.replace('_', '').upper(), raw.upper())


def _is_gguf(row) -> bool:
    """``is_gguf_model()`` against the columns available here."""
    source = _source_name(row.source)
    if source == 'HUGGING_FACE' and row.huggingface_filename:
        return row.huggingface_filename.endswith('.gguf')
    if source == 'MODEL_SCOPE' and row.model_scope_file_path:
        return row.model_scope_file_path.endswith('.gguf')
    if source == 'LOCAL_PATH' and row.local_path:
        return row.local_path.endswith('.gguf')
    return False


def _derived_backend(row) -> str:
    """``get_backend()``: explicit column, else gguf → custom, else vLLM."""
    if row.backend:
        return row.backend
    return _GGUF_BACKEND if _is_gguf(row) else _FALLBACK_BACKEND


def upgrade() -> None:
    bind = op.get_bind()
    # A data migration has to survive schemas that predate the columns it reads.
    # Fork test suites (and anyone replaying only part of the chain) stamp an
    # upstream revision onto a database where ``models`` does not exist yet; there
    # is nothing to backfill in that case, and failing here would break them for
    # no gain.
    inspector = sa.inspect(bind)
    if not inspector.has_table('models'):
        return
    columns = {column['name'] for column in inspector.get_columns('models')}
    if not {
        'distributed_inference_across_workers',
        'backend',
        'source',
    } <= columns:
        return

    rows = bind.execute(
        sa.select(models).where(
            models.c.distributed_inference_across_workers.is_(None)
        )
    ).fetchall()
    for row in rows:
        bind.execute(
            models.update()
            .where(models.c.id == row.id)
            .values(
                distributed_inference_across_workers=(
                    _derived_backend(row) in _DISTRIBUTED_BACKENDS
                )
            )
        )


def downgrade() -> None:
    # Deliberately a no-op: the original NULLs are indistinguishable from "written
    # as NULL by hand", and restoring them would re-open the API/scheduler gap.
    pass
