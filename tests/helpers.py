from __future__ import annotations

from conftest import at

from questersboard.domain import events, roster

ORC_WAR = dict(edition="forever", ruleset="pvp", faction="horde", race_key="orc", class_key="warrior",
               eligible_roles=["tank", "damage"], preferred_role="tank")


def char(s, cfg, a, name, **kw):
    return roster.add_character(s, cfg, a, {**ORC_WAR, "name": name, **kw})


def event(s, cfg, a, start=0, hours=2, publish=True, **kw):
    data = {**dict(edition="forever", ruleset="pvp", faction="horde", title="Deadmines run",
                   activity_type="dungeon", start_at=at(start), end_at=at(start + hours),
                   timezone="America/Los_Angeles"), **kw}
    ev = events.create_draft(s, cfg, a, data)
    if publish:
        events.publish(s, cfg, a, ev.id)
    return ev
