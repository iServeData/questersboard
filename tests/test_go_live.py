"""deploy/go_live.sh against stand-ins (FIXTURE nginx, systemctl, curl, admin CLI and health check): it changes
the config and the site file only after its checks, puts everything back when nginx -t fails, and rolls back."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "go_live.sh"
URL = "https://community.example.org"
ANCHOR = "include /etc/nginx/snippets/site_hardening.conf;"
SITE = f"""server {{
    listen 443 ssl;
    server_name community.example.org;
    {ANCHOR}
    location / {{ try_files $uri $uri/ =404; }}
}}
"""
CONF = 'name = "Example Community"\n[community]\npublic_base_url = "https://prototype.example.org"   # was prototype\n'


def _stub(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


def _env(tmp: Path, nginx_ok: bool = True) -> dict:
    bin_ = tmp / "bin"
    bin_.mkdir(exist_ok=True)
    (tmp / "etc").mkdir(exist_ok=True)
    (tmp / "etc" / "config.toml").write_text(CONF)
    (tmp / "site").write_text(SITE)
    (tmp / "snip.conf").write_text("location ^~ /wow/quester/ {}\n")
    log = tmp / "calls.log"
    _stub(bin_, "nginx", f'echo "nginx $*" >> {log}; {"exit 0" if nginx_ok else "[ -f " + str(tmp) + "/broken ] && exit 1; grep -q questersboard " + str(tmp) + "/site && exit 1; exit 0"}\n')
    _stub(bin_, "systemctl", f'echo "systemctl $*" >> {log}; exit 0\n')
    _stub(bin_, "admin", f'echo "admin $*" >> {log}; grep -q "^public_base_url = \\"https://" {tmp}/etc/config.toml\n')
    _stub(bin_, "health", f'echo "health $*" >> {log}; exit 0\n')
    _stub(bin_, "sleep", "exit 0\n")
    _stub(bin_, "curl", f"""\
        out=""; fmt=""; url=""
        while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; -w) fmt="$2"; shift 2;; -s) shift;; *) url="$1"; shift;; esac; done
        echo "curl $url" >> {log}
        case "$url" in
          */status.json) body='{{"generated_at": "now"}}';;
          */privacy) body='<h1>Privacy</h1>';;
          *) body='ok';;
        esac
        [ -n "$out" ] && printf '%s' "$body" > "$out"
        if [ "$fmt" = '%{{redirect_url}}' ]; then
          printf 'https://discord.com/oauth2/authorize?redirect_uri=%s' "$(python3 -c 'import urllib.parse,re;print(urllib.parse.quote(re.search(r"public_base_url = \\"([^\\"]*)",open("{tmp}/etc/config.toml").read()).group(1)+"/wow/quester/oauth/callback",safe=""))')"
        else printf 200; fi
    """)
    return {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "WQ_ETC": str(tmp / "etc"),
            "WQ_SNIP": str(tmp / "snip.conf"), "WQ_ADMIN": str(bin_ / "admin"), "WQ_HEALTH": str(bin_ / "health"),
            "WQ_BACKUP_DIR": str(tmp)}


def _run(tmp, env, *args, answer="yes\n"):
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, input=answer, text=True, capture_output=True,
                          cwd=tmp)


def _args(tmp):
    return ["--url", URL, "--site-file", str(tmp / "site"), "--after", ANCHOR]


def test_go_live_switches_url_adds_include_and_checks(tmp_path):
    env = _env(tmp_path)
    r = _run(tmp_path, env, *_args(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f'public_base_url = "{URL}"   # was prototype' in (tmp_path / "etc" / "config.toml").read_text()
    site = (tmp_path / "site").read_text()
    assert f"    {ANCHOR}\n    # QuestersBoard" in site and "    include /etc/nginx/snippets/questersboard_location[s].conf;\n" in site
    assert "Live. Next: merge the site" in r.stdout and "CHECK" not in r.stdout
    calls = (tmp_path / "calls.log").read_text()
    assert "systemctl reload nginx" in calls and "systemctl restart questersboard-web questersboard-bot questersboard-worker" in calls
    # running again changes nothing more
    r2 = _run(tmp_path, env, *_args(tmp_path))
    assert r2.returncode == 0 and (tmp_path / "site").read_text().count("questersboard_location[s]") == 1


def test_nothing_changes_without_the_discord_confirmation(tmp_path):
    env = _env(tmp_path)
    r = _run(tmp_path, env, *_args(tmp_path), answer="no\n")
    assert r.returncode == 1 and "Nothing changed" in r.stderr
    assert (tmp_path / "etc" / "config.toml").read_text() == CONF and (tmp_path / "site").read_text() == SITE
    assert not list(tmp_path.glob("questersboard_go_live_*.tgz"))


def test_anchor_must_appear_exactly_once(tmp_path):
    env = _env(tmp_path)
    r = _run(tmp_path, env, "--url", URL, "--site-file", str(tmp_path / "site"), "--after", "include nothing;")
    assert r.returncode == 1 and "exactly one line" in r.stderr
    assert (tmp_path / "site").read_text() == SITE


def test_failed_nginx_test_puts_everything_back(tmp_path):
    env = _env(tmp_path, nginx_ok=False)
    r = _run(tmp_path, env, *_args(tmp_path))
    assert r.returncode == 1 and "Everything was put back" in r.stderr
    assert (tmp_path / "etc" / "config.toml").read_text() == CONF and (tmp_path / "site").read_text() == SITE
    assert "systemctl restart" not in (tmp_path / "calls.log").read_text()


def test_rollback_restores_the_backup(tmp_path):
    env = _env(tmp_path)
    assert _run(tmp_path, env, *_args(tmp_path)).returncode == 0
    backup = next(tmp_path.glob("questersboard_go_live_*.tgz"))
    r = _run(tmp_path, env, "--rollback", str(backup))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "etc" / "config.toml").read_text() == CONF and (tmp_path / "site").read_text() == SITE
