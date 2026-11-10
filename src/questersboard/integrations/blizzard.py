"""Edition-aware Blizzard Battle.net API adapter.

Ground rules:
* Every call names an edition; its namespaces come from config. If an edition has no
  namespace for a capability, or config marks the capability "unsupported", the call
  raises Unsupported with a clear message. It NEVER falls back to Retail, Classic Era,
  Anniversary or PTR namespaces. PTR namespaces are rejected at config load.
* A character lookup confirms that Blizzard returns a character with that name on that
  realm. It does NOT prove the Discord member owns it (status "api_matched").
  Ownership is only "ownership_verified" after an optional Battle.net login (wow.profile)
  lists the character on the member's own account. The user token is used once and
  discarded; only an encrypted snapshot of character keys is stored.
* Client-credentials token cached in memory; responses cached in api_cache; retries with
  exponential backoff and Retry-After on 429/5xx; a per-process rate limiter.
"""

from __future__ import annotations

import json
import logging
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import Config, Edition
from ..models import ApiCache, ApiProbe, BattlenetLink, Character, ReferenceOption
from ..secrets import get_secret
from ..domain.timeutil import now

log = logging.getLogger("questersboard.blizzard")


class Unsupported(Exception):
    """The edition/capability isn't available from Blizzard (or not configured). Not an outage."""


class ApiError(Exception):
    pass


@dataclass
class CharacterMatch:
    found: bool
    detail: str
    level: int | None = None
    race: str | None = None
    klass: str | None = None
    faction: str | None = None
    external_id: str | None = None
    namespace: str | None = None
    ruleset: str | None = None


class _Limiter:
    def __init__(self, per_second: int):
        self.interval = 1.0 / max(1, per_second)
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            t = time.monotonic()
            if t < self.next:
                time.sleep(self.next - t)
            self.next = max(t, self.next) + self.interval


def slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("'", "")


def char_slug(name: str) -> str:
    """Realmless (Forever) character key: "Grimtusk Ironhide" -> "grimtusk-ironhide"."""
    return "-".join(name.strip().lower().split())


_RULESET_WORDS = {"pvp": "pvp", "normal": "normal", "pve": "normal", "rp": "rp", "roleplay": "rp",
                  "role-playing": "rp", "roleplaying": "rp", "rppvp": "rp_pvp", "roleplaypvp": "rp_pvp",
                  "hardcore": "hardcore"}


def ruleset_key(ed: Edition, raw) -> str | None:
    """Map what Blizzard returns for a ruleset ({id, name}, a name or an ID) to one of the edition's keys.
    Unknown or unconfigured rulesets give None; the member then picks it."""
    if raw is None:
        return None
    rid, name = None, None
    if isinstance(raw, dict):
        rid, name = raw.get("id"), raw.get("name") or raw.get("type") or raw.get("key")
        if isinstance(name, dict):
            name = name.get("en_US") or next(iter(name.values()), None)
    elif isinstance(raw, int) and not isinstance(raw, bool):
        rid = raw
    else:
        name = str(raw)
    if isinstance(rid, int):
        hit = next((k for k, v in ed.ruleset_api_ids.items() if v == rid), None)
        if hit:
            return hit
    if name:
        squashed = "".join(ch for ch in str(name).lower() if ch.isalnum())
        key = _RULESET_WORDS.get(squashed) or str(name).strip().lower().replace("-", "_").replace(" ", "_")
        if key in ed.rulesets:
            return key
        for k in ed.rulesets:
            if "".join(ch for ch in ed.ruleset_label(k).lower() if ch.isalnum()) == squashed:
                return k
    return None


def _label(v) -> str:
    if isinstance(v, dict):
        v = v.get("name") if v.get("name") is not None else v.get("type")
        if isinstance(v, dict):
            v = v.get("en_US") or next(iter(v.values()), "")
    return str(v or "").strip()


def _shape(page: str, body: dict) -> str:
    """Field names and list sizes of an answer, plus a few non-personal highlights, for probe output."""
    if not isinstance(body, dict):
        return type(body).__name__
    parts = []
    for k, v in body.items():
        if k == "_links":
            continue
        if isinstance(v, list):
            parts.append(f"{k}[{len(v)}]")
        elif isinstance(v, dict) and set(v) == {"href"}:
            parts.append(f"{k}->")          # the summary links to a detail page
        else:
            parts.append(k)
    hi = []
    if page == "summary":
        hi += [f"level={body.get('level')}"] if "level" in body else []
        hi += [f"guild={_label(body['guild'])}"] if isinstance(body.get("guild"), dict) else ["no guild"]
        for k in ("average_item_level", "equipped_item_level"):
            if k in body:
                hi.append(f"{k}={body[k]}")
        if isinstance(body.get("active_spec"), dict):
            hi.append(f"active_spec={_label(body['active_spec'])}")
    elif page == "equipment" and isinstance(body.get("equipped_items"), list):
        lv = [((i.get("level") or {}).get("value")) for i in body["equipped_items"] if isinstance(i, dict)]
        lv = [x for x in lv if isinstance(x, int)]
        if lv:
            hi.append(f"item levels {min(lv)}-{max(lv)}")
    elif page == "professions":
        for grp in ("primaries", "secondaries"):
            for p in body.get(grp) or []:
                tiers = p.get("tiers") or [p]
                pts = [t.get("skill_points") for t in tiers if isinstance(t, dict) and t.get("skill_points")]
                hi.append(f"{_label(p.get('profession'))}{(' ' + str(max(pts))) if pts else ''}")
    elif page == "specializations" and isinstance(body.get("active_specialization"), dict):
        hi.append(f"active={_label(body['active_specialization'])}")
    return ", ".join(parts) + (f"  |  {'; '.join(hi)}" if hi else "")


_TREE_SKIP = {"_links", "character", "key", "href", "guild_crest"}


def _tree(v, depth: int = 0, max_depth: int = 6, max_items: int = 2) -> list[str]:
    """An answer's structure for probe output: nested field names, list sizes (first items only),
    numbers and short text. Links, the character block and crest art are left out."""
    pad = "  " * depth
    lines: list[str] = []
    if depth > max_depth:
        return [pad + "..."]
    if isinstance(v, dict):
        for k, x in v.items():
            if k in _TREE_SKIP:
                continue
            if isinstance(x, (dict, list)):
                lines.append(f"{pad}{k}{f'[{len(x)}]' if isinstance(x, list) else ''}:")
                lines += _tree(x, depth + 1, max_depth, max_items)
            else:
                lines.append(f"{pad}{k}: {_leaf(x)}")
    elif isinstance(v, list):
        for i, x in enumerate(v[:max_items]):
            lines.append(f"{pad}- [{i}]")
            lines += _tree(x, depth + 1, max_depth, max_items)
        if len(v) > max_items:
            lines.append(f"{pad}  (+{len(v) - max_items} more)")
    else:
        lines.append(pad + _leaf(v))
    return lines


def _leaf(x) -> str:
    if isinstance(x, str):
        return repr(x if len(x) <= 40 else x[:37] + "...")
    return repr(x)


class Blizzard:
    def __init__(self, cfg: Config, http: httpx.Client | None = None, sleep=time.sleep):
        self.cfg = cfg
        self.bz = cfg["blizzard"]
        self.http = http or httpx.Client(timeout=self.bz.get("timeout_seconds", 10))
        self.sleep = sleep
        self._token: tuple[str, float] | None = None
        self.limiter = _Limiter(self.bz.get("requests_per_second", 20))

    # ------------------------------------------------------------------ plumbing
    def require(self, ed: Edition, cap: str, ns_kind: str, region: str) -> str:
        if not self.bz.get("enabled"):
            raise Unsupported("Blizzard API lookups are turned off in this installation.")
        status = ed.capability(cap)
        ns = ed.namespace(ns_kind, region)
        if status == "unsupported" or not ns:
            raise Unsupported(f"Blizzard doesn't provide {cap} data for {ed.name} ({region.upper()}) yet. "
                              "Entries stay self-reported.")
        return ns

    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        cid = get_secret("blizzard-client-id")
        sec = get_secret("blizzard-client-secret")
        r = self._send("POST", f"{self.bz['oauth_base'].rstrip('/')}/token", auth=(cid.reveal(), sec.reveal()),
                       data={"grant_type": "client_credentials"})
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def _send(self, method, url, **kw) -> httpx.Response:
        tries = self.bz.get("max_retries", 3) + 1
        delay = 0.5
        last = None
        for i in range(tries):
            self.limiter.wait()
            try:
                r = self.http.request(method, url, **kw)
            except httpx.HTTPError as exc:
                last = f"network: {type(exc).__name__}"
            else:
                if r.status_code < 400 or r.status_code in (403, 404):
                    return r
                last = f"http {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = max(delay, min(float(ra), 30.0))
            if i + 1 < tries:
                self.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise ApiError(f"Blizzard API unavailable ({last}).")

    def get(self, s: Session | None, region: str, path: str, namespace: str, cache_hours: float,
            locale: str = "en_US") -> tuple[int, dict]:
        key = f"{region}|{namespace}|{path}|{locale}"[:300]
        if s is not None:
            hit = s.get(ApiCache, key)
            if hit and hit.expires_at > now():
                return hit.status, hit.body
        url = self.cfg["blizzard"]["api_host_template"].replace("{region}", region) + path
        r = self._send("GET", url, params={"namespace": namespace, "locale": locale},
                       headers={"Authorization": f"Bearer {self.token()}"})
        status = r.status_code
        body = r.json() if status == 200 else {}
        if s is not None and status in (200, 404):
            ttl = cache_hours if status == 200 else min(cache_hours, 1)
            vals = dict(key=key, status=status, body=body, fetched_at=now(), expires_at=now() + timedelta(hours=ttl))
            s.execute(insert(ApiCache).values(**vals).on_conflict_do_update(
                index_elements=[ApiCache.key], set_={k: v for k, v in vals.items() if k != "key"}))
        if status == 403:
            raise ApiError("Blizzard refused the request (403). Check the API client's access.")
        return status, body

    # ------------------------------------------------------------------ capabilities
    def realm_for(self, ed: Edition, ch_ruleset: str | None, ch_realm: str | None) -> str:
        if ed.uses_realms:
            if not ch_realm:
                raise Unsupported("A realm is required for this edition.")
            return slug(ch_realm)
        rs = ed.ruleset_realm_slugs.get(ch_ruleset or "", "")
        if not rs:
            raise Unsupported(f"{ed.name} has no realms, and the API realm slug for the "
                              f"{(ch_ruleset or '').upper()} ruleset hasn't been confirmed yet.")
        return rs

    def lookup_character(self, s: Session, ch: Character) -> CharacterMatch:
        ed = self.cfg.edition(ch.edition)
        ns = self.require(ed, "character", "profile", ch.region)
        path = self.character_path(ed, ch)
        status, body = self.get(s, ch.region, path, ns, self.bz.get("cache_hours_profile", 6))
        if status == 404:
            extra = "" if ed.uses_realms else " Forever characters show up from level 10, after you log out once."
            return CharacterMatch(False, "Blizzard has no public profile for that name (it may be new, "
                                         "private, or misspelled)." + extra, namespace=ns)
        return CharacterMatch(True, "Blizzard returned a character with this name.", level=body.get("level"),
                              race=_label(body.get("race")) or None,
                              klass=_label(body.get("character_class")) or None,
                              faction=((body.get("faction") or {}).get("type") or "").lower() or None,
                              external_id=str(body.get("id")) if body.get("id") else None, namespace=ns,
                              ruleset=None if ed.uses_realms else ruleset_key(ed, body.get("ruleset")))

    def character_path(self, ed: Edition, ch: Character) -> str:
        if ed.uses_realms:
            realm = self.realm_for(ed, ch.ruleset, ch.realm_slug)
            return f"/profile/wow/character/{quote(realm)}/{quote(ch.name.lower())}"
        # Realmless (Forever): /profile/wow/character/{name}-{surname}. Characters only appear from
        # level 10, after logging out once.
        return f"/profile/wow/character/{quote(char_slug(ch.name))}"

    def apply_lookup(self, s: Session, ch: Character) -> CharacterMatch:
        """Look up and record the result on the character. Never sets ownership_verified."""
        m = self.lookup_character(s, ch)
        if m.found:
            if ch.verification != "ownership_verified":
                ch.verification = "api_matched"
            ch.api_external_id, ch.api_namespace, ch.api_refreshed_at = m.external_id, m.namespace, now()
            if m.level:
                ch.level = m.level
        return m

    def probe(self, s: Session, edition: str, region: str) -> dict[str, str]:
        """Check what Blizzard actually serves for an edition. Results go to api_probes."""
        ed = self.cfg.edition(edition)
        checks = {"reference": ("static", "/data/wow/playable-class/index"),
                  "character": ("dynamic", "/data/wow/realm/index"),
                  "quests": ("static", "/data/wow/quest/area/index"),
                  "locations": ("static", "/data/wow/journal-instance/index")}
        if not ed.uses_realms:
            checks["character"] = ("dynamic", "/data/wow/ruleset/index")   # no realms; rulesets instead
        out = {}
        for cap, (kind, path) in checks.items():
            ns = ed.namespace(kind, region)
            if not ns:
                res, det = "unsupported", "no namespace configured (not published by Blizzard)"
            else:
                try:
                    st, _ = self.get(None, region, path, ns, 0)
                    res, det = ("ok", f"{ns} {path} -> 200") if st == 200 else (f"http_{st}", f"{ns} {path}")
                except (ApiError, Exception) as exc:  # noqa: BLE001 - probe reports everything
                    res, det = "error", str(exc)[:400]
            out[cap] = res
            vals = dict(edition=edition, region=region, capability=cap, result=res, detail=det, probed_at=now())
            s.execute(insert(ApiProbe).values(**vals).on_conflict_do_update(
                index_elements=[ApiProbe.edition, ApiProbe.region, ApiProbe.capability],
                set_={k: v for k, v in vals.items() if k not in ("edition", "region", "capability")}))
        out["ownership"] = "unsupported" if not ed.namespace("profile", region) else "needs_oauth"
        return out

    # The character profile and its detail pages. Forever's are undocumented; probe_character reports
    # which ones answer so the roster/guild features only use pages that really work.
    PROFILE_PAGES = (("summary", ""), ("equipment", "/equipment"), ("specializations", "/specializations"),
                     ("professions", "/professions"), ("character-media", "/character-media"),
                     ("statistics", "/statistics"), ("pvp-summary", "/pvp-summary"),
                     ("achievements", "/achievements"), ("titles", "/titles"), ("reputations", "/reputations"),
                     ("dungeons", "/encounters/dungeons"), ("raids", "/encounters/raids"),
                     ("quests", "/quests/completed"), ("mounts", "/collections/mounts"),
                     ("appearance", "/appearance"), ("hunter-pets", "/hunter-pets"))

    def probe_character(self, edition: str, region: str, name: str, realm: str | None = None,
                        detail: str | None = None) -> list[dict]:
        """Read-only: which profile pages Blizzard serves for one character, with the shape of each answer
        (field names and list sizes, a few highlights). Uses the app token; saves nothing, uses no cache.
        If the character is in a guild, the guild and guild roster pages are checked too (sizes only).
        detail = a page name: that page's structure is added as row["tree"] (see _tree)."""
        if not self.bz.get("enabled"):
            raise Unsupported("Blizzard API lookups are turned off in this installation.")
        ed = self.cfg.edition(edition)
        ns = ed.namespace("profile", region)
        if not ns:
            raise Unsupported(f"No profile namespace is configured for {ed.name} ({region.upper()}).")
        if ed.uses_realms:
            if not realm:
                raise Unsupported(f"{ed.name} uses realms: add --realm.")
            base = f"/profile/wow/character/{quote(slug(realm))}/{quote(name.strip().lower())}"
        else:
            base = f"/profile/wow/character/{quote(char_slug(name))}"
        out, summary_guild = [], ""
        for page, suffix in self.PROFILE_PAGES:
            row = {"page": page, "path": base + suffix, "namespace": ns}
            body = self._probe_row(row, region, base + suffix, ns, page)
            if page == detail and row["status"] == "200":
                row["tree"] = _tree(body)
            if page == "summary" and row["status"] == "200":
                href = ((body.get("guild") or {}).get("key") or {}).get("href") or ""
                summary_guild = href
            out.append(row)
        g = urlsplit(summary_guild) if out and out[0]["status"] == "200" and summary_guild else None
        if g and g.path.startswith("/data/wow/guild/"):
            gns = dict(parse_qsl(g.query)).get("namespace") or ed.namespace("profile", region) or ns
            for page, path in (("guild", g.path), ("guild-roster", g.path.rstrip("/") + "/roster")):
                row = {"page": page, "path": path, "namespace": gns}
                body = self._probe_row(row, region, path, gns, page)
                if page == detail == "guild" and row["status"] == "200":
                    row["tree"] = _tree(body)        # the roster's tree is never shown: it lists other players
                out.append(row)
        return out

    def _probe_row(self, row: dict, region: str, path: str, ns: str, page: str) -> dict:
        try:
            st, body = self.get(None, region, path, ns, 0)
            row["status"] = str(st)
            row["shape"] = _shape(page, body) if st == 200 else ""
            return body if st == 200 else {}
        except (ApiError, ValueError, httpx.HTTPError) as exc:
            row["status"], row["shape"] = "error", (str(exc) or type(exc).__name__)[:160]
            return {}

    def import_reference(self, s: Session, edition: str, region: str) -> dict[str, int]:
        """Classes, races, quest areas, dungeon/raid instances. Rows marked source=blizzard, verified."""
        ed = self.cfg.edition(edition)
        ns = self.require(ed, "reference", "static", region)
        hours = self.bz.get("cache_hours_static", 168)
        counts = {}
        plan = [("class", "/data/wow/playable-class/index", "classes"),
                ("race", "/data/wow/playable-race/index", "races")]
        if ed.capability("quests") != "unsupported":
            plan.append(("zone", "/data/wow/quest/area/index", "areas"))
        if ed.capability("locations") != "unsupported":
            plan.append(("location", "/data/wow/journal-instance/index", "instances"))
        for kind, path, field in plan:
            st, body = self.get(s, region, path, ns, hours)
            if st != 200:
                counts[kind] = 0
                continue
            n = 0
            for item in body.get(field, []):
                name = item.get("name")
                if isinstance(name, dict):
                    name = name.get("en_US")
                if not name:
                    continue
                key = slug(name).replace("-", "_")
                data = self._instance_details(s, region, ns, hours, item) if kind == "location" else {}
                vals = dict(edition=edition, kind=kind, key=key, label=name[:160],
                            parent_key=data.get("category", "") if kind == "location" else "", source="blizzard",
                            source_ref=f"{ns}{path}", external_id=str(item.get("id", "")),
                            verification="verified", data=data, updated_at=now())
                s.execute(insert(ReferenceOption).values(**vals).on_conflict_do_update(
                    constraint="ux_ref_key", set_={k: v for k, v in vals.items()
                                                   if k in ("label", "source", "source_ref", "external_id",
                                                            "verification", "updated_at", "data",
                                                            "parent_key")}))
                n += 1
            counts[kind] = n
        return counts

    def _instance_details(self, s: Session, region: str, ns: str, hours: float, item: dict) -> dict:
        """Difficulties and minimum level for one dungeon/raid, from /data/wow/journal-instance/{id}.

        Blizzard lists the modes (Normal, Heroic, Mythic, Mythic Keystone, LFR, legacy raid sizes) and an
        optional minimum_level. Missing fields are simply left out; the event form then lets the organizer
        pick from the edition's configured difficulties and type the level range.
        """
        if not item.get("id") or not self.bz.get("instance_details", True):
            return {}
        st, body = self.get(s, region, f"/data/wow/journal-instance/{int(item['id'])}", ns, hours)
        if st != 200:
            return {}
        out: dict = {}
        cat = (body.get("category") or {}).get("type")
        if cat in ("DUNGEON", "RAID", "EVENT", "WORLD_BOSS"):
            out["category"] = cat.lower()
        modes = []
        for m in body.get("modes") or []:
            name = ((m.get("mode") or {}).get("name"))
            if isinstance(name, dict):
                name = name.get("en_US")
            if name and name not in modes:
                modes.append(str(name)[:32])
        if modes:
            out["modes"] = modes
        if isinstance(body.get("minimum_level"), int):
            out["minimum_level"] = body["minimum_level"]
        return out

    # ------------------------------------------------------------------ optional ownership (Battle.net login)
    def authorize_url(self, region: str, redirect_uri: str) -> tuple[str, str]:
        if not self.bz.get("battlenet_link_enabled"):
            raise Unsupported("Battle.net account linking is turned off.")
        state = pysecrets.token_urlsafe(24)
        q = urlencode({"client_id": get_secret("blizzard-client-id").reveal(), "scope": "wow.profile",
                       "state": state, "redirect_uri": redirect_uri, "response_type": "code"})
        return f"{self.bz['oauth_base'].rstrip('/')}/authorize?{q}", state

    def account_characters(self, region: str, ns: str, user_token: str) -> list[dict]:
        """The characters on the signed-in Battle.net account (/profile/user/wow, user token)."""
        url = self.cfg["blizzard"]["api_host_template"].replace("{region}", region) + "/profile/user/wow"
        r = self._send("GET", url, params={"namespace": ns, "locale": "en_US"},
                       headers={"Authorization": f"Bearer {user_token}"})
        if r.status_code == 404:
            return []          # no characters for this edition and region on the account
        if r.status_code != 200:
            raise ApiError(f"Couldn't read the Battle.net account ({r.status_code}).")
        out = []
        for acct in r.json().get("wow_accounts", []) or []:
            for c in acct.get("characters", []) or []:
                first, last = str(c.get("name") or "").strip(), str(c.get("surname") or "").strip()
                if not first:
                    continue
                full = f"{first} {last}".strip()
                out.append({"id": str(c.get("id") or ""), "name": full, "slug": char_slug(full),
                            "realm": (c.get("realm") or {}).get("slug", "") or "",
                            "level": c.get("level") if isinstance(c.get("level"), int) else None,
                            "race": _label(c.get("playable_race")), "class": _label(c.get("playable_class")),
                            "faction": _label(c.get("faction")).lower(), "ruleset_raw": c.get("ruleset")})
        return out

    def link_account(self, s: Session, member_id: int, edition: str, region: str, code: str,
                     redirect_uri: str) -> dict:
        """Exchange the code, read the account's character list once, mark matching characters verified
        (and bring their level up to date), and keep the rest as an encrypted list the member can add from.

        The user token is never stored (Battle.net user tokens last ~24h and have no refresh token).
        Returns {"verified": [names], "new": [entries not on the member's roster yet]}.
        """
        ed = self.cfg.edition(edition)
        ns = self.require(ed, "ownership", "profile", region)
        r = self._send("POST", f"{self.bz['oauth_base'].rstrip('/')}/token",
                       auth=(get_secret("blizzard-client-id").reveal(), get_secret("blizzard-client-secret").reveal()),
                       data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        if r.status_code != 200:
            raise ApiError("Battle.net login failed.")
        user_token = r.json()["access_token"]
        try:
            chars = self.account_characters(region, ns, user_token)
        finally:
            del user_token
        if ed.uses_realms:
            for c in chars:
                c.pop("ruleset_raw", None)
                c["ruleset"] = None
        else:
            self._fill_rulesets(s, ed, region, chars)
        key = get_secret("field-key")
        blob = Fernet(key.reveal().encode()).encrypt(json.dumps({"edition": ed.key, "chars": chars}).encode())
        vals = dict(member_id=member_id, region=region, snapshot_enc=blob, linked_at=now())
        s.execute(insert(BattlenetLink).values(**vals).on_conflict_do_update(
            index_elements=[BattlenetLink.member_id, BattlenetLink.region],
            set_={"snapshot_enc": blob, "linked_at": now()}))
        verified = []
        mine = s.scalars(select(Character).where(Character.member_id == member_id, Character.edition == edition,
                                                 Character.region == region, Character.archived_at.is_(None)))
        for ch in mine:
            hit = self._match(ed, ch, chars)
            if hit is None:
                continue
            if self._claimed_by_other(s, ed.key, region, hit["id"], member_id):
                continue
            ch.verification, ch.api_external_id, ch.api_refreshed_at = "ownership_verified", hit["id"] or None, now()
            if hit.get("level") and 1 <= hit["level"] <= ed.max_level:
                ch.level = hit["level"]
            verified.append(ch.name)
        s.flush()
        return {"verified": verified, "new": self.offer(s, member_id, ed.key, region, chars)}

    def offer(self, s: Session, member_id: int, edition: str, region: str, chars: list[dict]
              ) -> list[tuple[int, dict]]:
        """Snapshot entries (with their index) that aren't on the member's roster yet and that nobody else
        has verified as theirs."""
        ed = self.cfg.edition(edition)
        mine = list(s.scalars(select(Character).where(
            Character.member_id == member_id, Character.edition == edition, Character.region == region,
            Character.archived_at.is_(None))))
        out = []
        for i, c in enumerate(chars):
            if any((c.get("id") and ch.api_external_id == c["id"]) or self._match(ed, ch, [c]) for ch in mine):
                continue
            if self._claimed_by_other(s, edition, region, c.get("id") or "", member_id):
                continue
            out.append((i, c))
        return out

    def _match(self, ed: Edition, ch: Character, chars: list[dict]) -> dict | None:
        if ed.uses_realms:
            try:
                realm = self.realm_for(ed, ch.ruleset, ch.realm_slug)
            except Unsupported:
                return None
            return next((c for c in chars if c["name"].lower() == ch.name.lower() and c["realm"] == realm), None)
        # Realmless: a first name + surname is unique per region, so the slug alone identifies it.
        want = char_slug(ch.name)
        return next((c for c in chars if c["slug"] == want), None)

    @staticmethod
    def _claimed_by_other(s: Session, edition: str, region: str, external_id: str, member_id: int) -> bool:
        if not external_id:
            return False
        return s.scalar(select(Character.id).where(
            Character.edition == edition, Character.region == region, Character.api_external_id == external_id,
            Character.verification == "ownership_verified", Character.archived_at.is_(None),
            Character.member_id != member_id).limit(1)) is not None

    def _fill_rulesets(self, s: Session, ed: Edition, region: str, chars: list[dict]) -> None:
        """Forever: the account list may not say which ruleset a character is on; the character profile does.
        Read it with the app token where the edition's character API is switched on. Failures just leave the
        ruleset for the member to pick."""
        budget = int(self.bz.get("battlenet_profile_lookups", 25))
        ns = ed.namespace("profile", region)
        for c in chars:
            c["ruleset"] = ruleset_key(ed, c.pop("ruleset_raw", None))
            if c["ruleset"] or budget <= 0 or ed.capability("character") == "unsupported" or not ns:
                continue
            budget -= 1
            try:
                st, body = self.get(s, region, f"/profile/wow/character/{quote(c['slug'])}", ns,
                                    self.bz.get("cache_hours_profile", 6))
            except ApiError:
                continue
            if st == 200:
                c["ruleset"] = ruleset_key(ed, body.get("ruleset"))
                c["race"] = c["race"] or _label(body.get("race"))
                c["class"] = c["class"] or _label(body.get("character_class"))
                c["faction"] = c["faction"] or _label(body.get("faction")).lower()
                if not c.get("level") and isinstance(body.get("level"), int):
                    c["level"] = body["level"]

    def read_snapshot(self, s: Session, member_id: int, region: str, max_age_minutes: float | None = None
                      ) -> tuple[str, list[dict]] | None:
        """(edition, characters) from the member's encrypted snapshot, or None if missing or too old."""
        row = s.get(BattlenetLink, (member_id, region))
        if row is None:
            return None
        if max_age_minutes is not None and row.linked_at < now() - timedelta(minutes=max_age_minutes):
            return None
        data = json.loads(Fernet(get_secret("field-key").reveal().encode()).decrypt(bytes(row.snapshot_enc)))
        if isinstance(data, list):         # snapshots from before Battle.net sync: keys only, no edition
            return None
        return data.get("edition", ""), list(data.get("chars", []))
