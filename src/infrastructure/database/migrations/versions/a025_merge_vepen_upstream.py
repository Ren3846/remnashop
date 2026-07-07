"""merge VEPEN fork branch (a021-a024) with upstream branch (0021-0024)

Revision ID: a025
Revises: 0024, a024
Create Date: 2026-07-07
"""

from typing import Sequence, Union

revision: str = "a025"
down_revision: Union[str, tuple[str, ...], None] = ("0024", "a024")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
