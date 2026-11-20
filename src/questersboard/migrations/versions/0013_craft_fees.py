"""what a crafter asks for a Can craft offer (Profession help)

Revision ID: 0013
Revises: 0012

- board_posts.fee_kind: "mats" (bring your materials, no fee), "mats_fee" (bring your materials,
  plus a fee) or "fee" (a fee, materials included); NULL = not said (posts made before this).
- board_posts.fee_copper: the fee in copper (100 copper = 1 silver, 100 silver = 1 gold), shown in
  gold, silver and copper like the auction house; NULL unless fee_kind asks for one.
The app role already has full access to board_posts; the worker only reads these.
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('board_posts', sa.Column('fee_kind', sa.String(length=10), nullable=True))
    op.add_column('board_posts', sa.Column('fee_copper', sa.BigInteger(), nullable=True))
    op.create_check_constraint('ck_board_posts_fee', 'board_posts',
                               "(fee_kind IS NULL OR fee_kind IN ('mats', 'mats_fee', 'fee')) AND "
                               "(fee_copper IS NULL OR fee_copper > 0)")


def downgrade() -> None:
    op.drop_constraint('ck_board_posts_fee', 'board_posts', type_='check')
    op.drop_column('board_posts', 'fee_copper')
    op.drop_column('board_posts', 'fee_kind')
