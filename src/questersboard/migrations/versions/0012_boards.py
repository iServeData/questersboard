"""Adventure Board and profession help board ([adventures], [professions])

Revision ID: 0012
Revises: 0011

- board_posts: a member's post on one of the two boards (a goal, an offer of help, looking for
  company; or a crafted item they need, a recipe they can craft, a recipe question, a farming
  request). Members only; never public. Open until the poster closes it or it runs out.
- board_responses: who answered a post and how (I can help, I can make this, an answer...). The
  poster sees who; everyone else sees counts.
The app role writes both. The worker closes posts that ran out and keeps their Discord cards.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

GRANTS = r"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON board_posts, board_responses TO questersboard_app;
    GRANT USAGE, SELECT ON SEQUENCE board_posts_id_seq TO questersboard_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_worker') THEN
    GRANT SELECT ON board_posts, board_responses TO questersboard_worker;
    GRANT UPDATE (state, closed_reason, updated_at, discord_channel_id, discord_message_id)
      ON board_posts TO questersboard_worker;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_readonly') THEN
    GRANT SELECT ON board_posts, board_responses TO questersboard_readonly;
  END IF;
END
$grants$;
"""


def upgrade() -> None:
    op.create_table(
        'board_posts',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('public_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('board', sa.String(length=12), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False),
        sa.Column('character_id', sa.BigInteger(), nullable=True),
        sa.Column('edition', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=120), nullable=False),
        sa.Column('body', sa.String(length=600), server_default='', nullable=False),
        sa.Column('activity_type', sa.String(length=24), nullable=True),
        sa.Column('profession', sa.String(length=40), nullable=True),
        sa.Column('recipe_name', sa.String(length=120), nullable=True),
        sa.Column('tags', postgresql.ARRAY(sa.String(length=40)), server_default='{}', nullable=False),
        sa.Column('windows', postgresql.JSONB(), server_default='[]', nullable=False),
        sa.Column('timezone', sa.String(length=64), server_default='UTC', nullable=False),
        sa.Column('group_size', sa.SmallInteger(), nullable=True),
        sa.Column('state', sa.String(length=10), server_default='open', nullable=False),
        sa.Column('closed_reason', sa.String(length=40), server_default='', nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_id', sa.BigInteger(), nullable=True),
        sa.Column('discord_channel_id', sa.String(length=22), nullable=True),
        sa.Column('discord_message_id', sa.String(length=22), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("board IN ('adventure','profession')", name='ck_board_posts_board'),
        sa.CheckConstraint("(board = 'adventure' AND kind IN ('need_help','offer_help','company')) OR "
                           "(board = 'profession' AND kind IN ('need_crafted','can_craft','recipe','farm'))",
                           name='ck_board_posts_kind'),
        sa.CheckConstraint("state IN ('open','done','closed','removed')", name='ck_board_posts_state'),
        sa.CheckConstraint('char_length(title) BETWEEN 3 AND 120', name='ck_board_posts_title'),
        sa.CheckConstraint('group_size IS NULL OR group_size BETWEEN 2 AND 80', name='ck_board_posts_group'),
        sa.ForeignKeyConstraint(['member_id'], ['members.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['character_id'], ['characters.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
    )
    op.create_index('ix_board_posts_open', 'board_posts', ['board', 'state', 'expires_at'])
    op.create_index('ix_board_posts_member_id', 'board_posts', ['member_id'])
    op.create_table(
        'board_responses',
        sa.Column('post_id', sa.BigInteger(), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=12), nullable=False),
        sa.Column('character_id', sa.BigInteger(), nullable=True),
        sa.Column('note', sa.String(length=400), server_default='', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind IN ('need','help','can_make','ask','answer')", name='ck_board_responses_kind'),
        sa.ForeignKeyConstraint(['post_id'], ['board_posts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['member_id'], ['members.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['character_id'], ['characters.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('post_id', 'member_id', 'kind'),
    )
    op.create_index('ix_board_responses_member_id', 'board_responses', ['member_id'])
    op.execute(GRANTS)


def downgrade() -> None:
    op.drop_table('board_responses')
    op.drop_table('board_posts')
