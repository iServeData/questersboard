# Operations

## Services

| Unit | Runs | DB role | Notes |
| --- | --- | --- | --- |
| `wowquester-web.socket` | `/run/wowquester-web.sock` (group `www-data`, 0660) | | Nginx connects here |
| `wowquester-web.service` | FastAPI app (socket-activated, `fd:3`) | `wowquester_app` | member pages, chronicle, feeds, calendars |
| `wowquester-bot.service` | Discord gateway | `wowquester_app` | slash commands and buttons only |
| `wowquester-worker.service` | outbox, reminders, overrun nudges, projections, housekeeping | `wowquester_worker` | all Discord posts and DMs |
| `wowquester-backup.timer` | nightly encrypted backup | `wowquester_admin` | |

All run as the `wowquester` system user with systemd sandboxing (read-only system, private tmp, no new
privileges, restricted address families, `InaccessiblePaths=` for other services). Each unit only receives
the credentials it needs.

```sh
systemctl status 'wowquester-*'
journalctl -u wowquester-web -u wowquester-bot -u wowquester-worker --since -1h
sudo /opt/wowquester/current/deploy/health.sh       # DB + outbox + web socket (loads the app credential)
sudo -u wowquester wowquester-admin check-config    # after editing config.toml
```

## Routine tasks

| Task | How |
| --- | --- |
| Change roles, limits, reminders, chronicle rules | edit `config.toml`; live sections apply within ~30 s (see `configuration.md`) |
| Other config changes | edit, `check-config`, `systemctl restart wowquester-web wowquester-bot wowquester-worker` |
| See stuck Discord deliveries (with the last error for each dead one) | `wowquester-admin outbox status` |
| Retry dead deliveries (after fixing the cause) | `wowquester-admin outbox retry-dead` |
| Give up on dead deliveries that no longer matter | `wowquester-admin outbox dismiss-dead` |
| Health check | `sudo ./deploy/health.sh` fails on dead deliveries; `install.sh`/`rollback.sh` use `--deploy`, which only warns, so a Discord permission problem can't roll back a working release |
| Revoke someone's private calendar link | `wowquester-admin calendar-revoke <discord id>` |
| Check WoW channel permissions and slash-command registration (read-only) | `wowquester-admin discord-permissions` (needs the `discord-bot-token` credential; `--category <id>` to pick another category) |
| Remove a member who can't log in (same as "Remove me") | `wowquester-admin erase-member <discord id>` shows the plan; add `--yes` to carry it out |
| Prune old audit rows / signups / sessions | `wowquester-admin prune` (admin role; monthly is plenty) |
| Re-import Blizzard reference data | `wowquester-admin blizzard import-reference retail --region us` |
| Check whether Forever's API exists yet | `wowquester-admin blizzard probe forever --region us` (reports, changes nothing) |
| Backup now | `sudo systemctl start wowquester-backup.service` |
| Restore drill | `backup_restore.md` |

## Health and failure behaviour

- **Discord down or rate-limited**: actions still succeed in the database; posts and DMs wait in the
  outbox and retry with backoff, honouring Discord's `retry_after`. After repeated failures a job is marked
  dead and shown by `outbox status`.
- **Event post deleted in Discord**: the worker recreates it on the next update.
- **Blizzard down**: lookups show "Blizzard's API didn't answer", nothing changes; planning continues with
  free text.
- **Database down**: `/healthz` returns 503; the WoW page keeps its static schedule; the bot answers
  "Something went wrong on our side. Please try again in a moment." and logs the error.
- **Bad config edit**: live reload keeps the old settings and logs the problem; a restart refuses to start
  (exit 78) instead of running with a broken config.
- **Event runs long**: managers get one DM after the grace period; nothing is extended automatically.

## Logs

journald only. Secrets are wrapped so accidental logging shows a fingerprint; Discord tokens and
`Authorization` headers are redacted by a log filter. Audit history is in the `audit_log` table
(who, what, when, before/after summary), not in journald.
