"""which of a goal's times work for each person who answered it (Adventure Board)

Revision ID: 0014
Revises: 0013

- board_responses.windows: the positions (0, 1, 2) of the post's times that work for this person;
  NULL = every time (answers made before this, or nobody picked). The page and the Discord card show
  how many people each time works for, so the poster can pick the best one.
The app role already has full access to board_responses and the worker reads the whole table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('board_responses', sa.Column('windows', postgresql.ARRAY(sa.SmallInteger()), nullable=True))


def downgrade() -> None:
    op.drop_column('board_responses', 'windows')
