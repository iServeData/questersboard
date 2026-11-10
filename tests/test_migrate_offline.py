"""`questersboard-admin migrate up --sql` must print SQL without a database password (install step 6 review)."""
from __future__ import annotations

import os
import subprocess
import sys

from conftest import ROOT


def test_migrate_sql_needs_no_password(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUESTERSBOARD_SECRET_")}
    env.pop("CREDENTIALS_DIRECTORY", None)
    env["PYTHONPATH"] = str(ROOT / "src")
    # an unreachable admin URL proves nothing connects
    cfg = (ROOT / "deploy" / "config.example.toml").read_text().replace("127.0.0.1:5432", "127.0.0.1:9")
    p = tmp_path / "config.toml"
    p.write_text(cfg)
    r = subprocess.run([sys.executable, "-c", "import sys; from questersboard.cli import main; sys.exit(main())",
                        "--config", str(p), "migrate", "up", "--sql"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "CREATE TABLE events" in r.stdout
    assert "INSERT INTO alembic_version" in r.stdout
