"""character details imported from Blizzard

Revision ID: 0009
Revises: 0008

- character_profiles: one row per character with the groups the member chose to import (gear,
  talents, professions, stats, portrait, PvP, progress, guild) and who each group is shared with
  (private | guild | members). Deleted with the character (cascade), by the app when the character is
  archived, renamed or erased, and by the worker once it's older than [blizzard] data_ttl_days.
- The worker can only see when a row was fetched and delete it.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

GRANTS = r"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON character_profiles TO questersboard_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_worker') THEN
    GRANT SELECT (character_id, fetched_at), DELETE ON character_profiles TO questersboard_worker;
  END IF;
END
$grants$;
"""


def upgrade() -> None:
    op.create_table(
        'character_profiles',
        sa.Column('character_id', sa.BigInteger(), sa.ForeignKey('characters.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('member_id', sa.BigInteger(), sa.ForeignKey('members.id'), nullable=False),
        sa.Column('edition', sa.String(32), nullable=False),
        sa.Column('region', sa.String(4), nullable=False),
        sa.Column('guild_id', sa.String(64), nullable=True),
        sa.Column('guild_name', sa.String(96), nullable=True),
        sa.Column('groups', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('shares', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_character_profiles_member_id', 'character_profiles', ['member_id'])
    op.create_index('ix_char_profile_guild', 'character_profiles', ['edition', 'region', 'guild_id'])
    op.execute(GRANTS)


def downgrade() -> None:
    op.drop_table('character_profiles')
