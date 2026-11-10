"""merge fork (video tasks / runtime configs) and upstream v2.3.0 heads

Revision ID: d9f0a1b2c3e4
Revises: b8d4f6a2c1e9, a7e2c9d4f681
Create Date: 2026-11-10 12:00:00.000000

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'd9f0a1b2c3e4'
down_revision: Union[str, Sequence[str], None] = ('b8d4f6a2c1e9', 'a7e2c9d4f681')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Merge point only. The two branches touch disjoint tables.
    pass


def downgrade() -> None:
    pass
