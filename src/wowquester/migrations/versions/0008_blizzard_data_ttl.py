"""Blizzard data time limit (API terms: keep Blizzard data no longer than 30 days)

Revision ID: 0008
Revises: 0007

The worker now removes Battle.net character lists once the "add these characters" window has passed
and clears Blizzard checks that haven't been refreshed within [blizzard] data_ttl_days. It gets only
what that needs: it can see when a list was made (not the encrypted list itself) and delete it, and it
can reset the Blizzard check columns on characters. No table changes.
"""
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

CHAR_COLS = "verification, api_external_id, api_namespace, api_refreshed_at, updated_at"

GRANTS = rf"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_worker') THEN
    GRANT SELECT (member_id, region, linked_at), DELETE ON battlenet_links TO wowquester_worker;
    GRANT UPDATE ({CHAR_COLS}) ON characters TO wowquester_worker;
  END IF;
END
$grants$;
"""

REVOKES = rf"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_worker') THEN
    REVOKE SELECT (member_id, region, linked_at), DELETE ON battlenet_links FROM wowquester_worker;
    REVOKE UPDATE ({CHAR_COLS}) ON characters FROM wowquester_worker;
  END IF;
END
$grants$;
"""


def upgrade() -> None:
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(REVOKES)
