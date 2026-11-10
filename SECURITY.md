# Security

## Reporting a problem

Please don't open a public issue for a security problem. Use **Report a vulnerability** on the
repository's Security tab, which reaches the maintainers privately. Include what you found, how to reproduce it, and the release or commit you're running
(`git -C ~/questersboard log -1 --oneline` on the host).

## What QuestersBoard protects

- **Secrets**: the Discord bot token and client secret, database passwords, the session key and any
  Blizzard credentials are loaded from systemd encrypted credentials (`LoadCredentialEncrypted=`), never
  from the config file or the environment, and never logged.
- **The database**: four roles with least privilege (admin for migrations, app, worker, read-only);
  the audit log is append-only for the app; `postgres` keeps local peer login only. See
  `docs/permissions_data_model.md`.
- **Members**: Discord login asks only for `identify`; nobody is named in a public Chronicle story
  without opting in; members can remove themselves (Remove me) and organizers can do it for them
  (`questersboard-admin erase-member`).
- **The web app**: CSRF tokens on every form, a strict Content-Security-Policy (no inline script),
  rate limits on writes, and uploads re-encoded with metadata stripped.

## Deployment checklist

- Keep the systemd sandboxing in `deploy/systemd/` (`systemd-analyze security questersboard-web`).
- Keep code and the virtualenv root-owned; the service user can't modify them (`deploy/install.sh`).
- Install libraries only with `--require-hashes` (the installer and CI do).
- Never commit real config, tokens, Discord IDs, host names or community names; CI runs
  `tools/check_sanitized.py` with a private denylist.

## Supported versions

Only the latest release gets fixes.
