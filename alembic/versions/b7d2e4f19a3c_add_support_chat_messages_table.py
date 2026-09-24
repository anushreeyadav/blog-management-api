"""add support chat messages table

Revision ID: b7d2e4f19a3c
Revises: 23f1d9da861c
Create Date: 2026-09-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e4f19a3c'
down_revision: Union[str, Sequence[str], None] = '23f1d9da861c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Purely additive -- a brand new table, no existing table/column/data
    # is touched.
    op.create_table('support_chat_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('response', sa.Text(), nullable=False),
    sa.Column('response_source', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_support_chat_messages_id'), 'support_chat_messages', ['id'], unique=False)
    op.create_index(op.f('ix_support_chat_messages_user_id'), 'support_chat_messages', ['user_id'], unique=False)
    op.create_index('ix_support_chat_messages_user_id_created_at', 'support_chat_messages', ['user_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_support_chat_messages_user_id_created_at', table_name='support_chat_messages')
    op.drop_index(op.f('ix_support_chat_messages_user_id'), table_name='support_chat_messages')
    op.drop_index(op.f('ix_support_chat_messages_id'), table_name='support_chat_messages')
    op.drop_table('support_chat_messages')
