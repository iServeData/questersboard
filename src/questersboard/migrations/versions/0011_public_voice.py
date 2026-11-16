"""open voice channels on the status card ([public_voice])

Revision ID: 0011
Revises: 0010

- public_voice_channels: for each channel in [public_voice] channels, its name and how many people
  (not bots) are in it, when it filled up and when it was last in use. Counts only, never who.
  The bot (app role) writes it; status.json reads it.
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

GRANTS = r"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON public_voice_channels TO questersboard_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_worker') THEN
    GRANT SELECT ON public_voice_channels TO questersboard_worker;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_readonly') THEN
    GRANT SELECT ON public_voice_channels TO questersboard_readonly;
  END IF;
END
$grants$;
"""


def upgrade() -> None:
    op.create_table(
        'public_voice_channels',
        sa.Column('channel_id', sa.String(length=22), nullable=False),
        sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=True),
        sa.Column('in_voice', sa.Integer(), nullable=True),
        sa.Column('occupied_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_active_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('in_voice IS NULL OR in_voice >= 0', name='ck_public_voice_nonneg'),
        sa.PrimaryKeyConstraint('channel_id'),
    )
    op.execute(GRANTS)


def downgrade() -> None:
    op.drop_table('public_voice_channels')
