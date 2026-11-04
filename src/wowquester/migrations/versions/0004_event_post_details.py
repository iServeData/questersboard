"""event post details: objective, difficulty, level range, cover art, host character, chat thread

Revision ID: 0004
Revises: 0003

- events: objective, difficulty, level_min/level_max, cover_image (a file name in the
  covers folder), host_character_id (the character shown as "Hosted by"), discord_thread_id
  (the per-event chat thread, when [event_threads] is on) and discord_thread_members (the members
  the bot added, so it can remove people who leave). All optional; existing events
  keep working and simply show the default cover until an organizer adds details.
- chronicle_entries.banner_upload_id: the screenshot an author picked as the story banner.
- public_chronicle.banner / details: the banner image and the difficulty line for the page.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


# The worker opens, fills and closes event chat threads, so it may update just these two columns.
GRANTS = r"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_worker') THEN
    GRANT UPDATE (discord_thread_id, discord_thread_members) ON events TO wowquester_worker;
  END IF;
END
$grants$;
"""
REVOKES = GRANTS.replace("GRANT UPDATE", "REVOKE UPDATE").replace(" TO wowquester_worker", " FROM wowquester_worker")


def upgrade() -> None:
    op.add_column('events', sa.Column('objective', sa.String(length=500), server_default=sa.text("''"),
                                      nullable=False))
    op.add_column('events', sa.Column('difficulty', sa.String(length=32), server_default=sa.text("''"),
                                      nullable=False))
    op.add_column('events', sa.Column('level_min', sa.SmallInteger(), nullable=True))
    op.add_column('events', sa.Column('level_max', sa.SmallInteger(), nullable=True))
    op.add_column('events', sa.Column('cover_image', sa.String(length=80), nullable=True))
    op.add_column('events', sa.Column('host_character_id', sa.BigInteger(),
                                      sa.ForeignKey('characters.id', ondelete='SET NULL'), nullable=True))
    op.add_column('events', sa.Column('discord_thread_id', sa.String(length=22), nullable=True))
    op.add_column('events', sa.Column('discord_thread_members', postgresql.JSONB(), server_default=sa.text("'[]'"),
                                      nullable=False))
    op.create_check_constraint('ck_event_levels', 'events',
                               'level_min is null or level_max is null or level_min <= level_max')
    op.add_column('chronicle_entries', sa.Column('banner_upload_id', sa.BigInteger(),
                                                 sa.ForeignKey('chronicle_uploads.id', ondelete='SET NULL'),
                                                 nullable=True))
    op.add_column('public_chronicle', sa.Column('banner', postgresql.JSONB(), nullable=True))
    op.add_column('public_chronicle', sa.Column('details', sa.String(length=120), server_default=sa.text("''"),
                                                nullable=False))
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(REVOKES)
    op.drop_column('public_chronicle', 'details')
    op.drop_column('public_chronicle', 'banner')
    op.drop_column('chronicle_entries', 'banner_upload_id')
    op.drop_constraint('ck_event_levels', 'events', type_='check')
    for c in ('discord_thread_members', 'discord_thread_id', 'host_character_id', 'cover_image', 'level_max', 'level_min', 'difficulty',
              'objective'):
        op.drop_column('events', c)
