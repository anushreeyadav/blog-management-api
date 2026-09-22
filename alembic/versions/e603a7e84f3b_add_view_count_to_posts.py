"""add view_count to posts

Revision ID: e603a7e84f3b
Revises: 94dcbecc071b
Create Date: 2026-09-22 11:03:40.563670

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e603a7e84f3b'
down_revision: Union[str, Sequence[str], None] = '94dcbecc071b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'posts',
        sa.Column('view_count', sa.Integer(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('posts', 'view_count')
