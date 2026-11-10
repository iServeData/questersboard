# Contributing

Thanks for helping. QuestersBoard is a self-hosted Discord + web companion for one World of Warcraft
community; everything deployment-specific lives in that community's `/etc/questersboard/config.toml`, so the
repository itself works for any community.

## Get it running

Clone the repository (the **Code** button on GitHub), then from its folder:

```sh
uv venv -p 3.12 .venv
uv pip install -r requirements.lock -r requirements-dev.lock && uv pip install -e . --no-deps
```

The tests need a disposable local PostgreSQL (16 or newer) with the four QuestersBoard roles. Setting one
up takes a few commands: see [docs/testing.md](docs/testing.md). Then:

```sh
set -a; . /path/outside/the/repo/test_pw.env; set +a
.venv/bin/python -m pytest -q tests          # check the summary: database tests are skipped, not passed, without the cluster
.venv/bin/python tools/check_sanitized.py
```

`tools/preview_server.py` serves the web pages with fixture data and no Discord (see the README).
Discord, Blizzard and AI providers are always FIXTURE fakes in tests; no test contacts a live service.

## Making a change

- Branch from `prototype` and open the pull request against `prototype`; `main` is what hosts install.
- Add or update tests with the change. CI runs the full suite against PostgreSQL and the sanitizer.
- Never commit real Discord IDs, tokens, passwords, domains, community, guild, channel, server or player
  names. Use `Example Community`, `community.example.org` and obviously fake IDs; a line with a fake
  17-20 digit ID needs the comment `sanitize: fixture`.
- Configurable behaviour (schedules, limits, wording) belongs in `config.toml`, not in code.
- Lowercase filenames with underscores, no spaces.
- The web pages use a strict Content Security Policy: no inline scripts or styles.

## Security problems

Please don't open a public issue; see [SECURITY.md](SECURITY.md).
