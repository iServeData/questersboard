import subprocess
import sys

from conftest import ROOT


def test_repository_contains_no_ids_tokens_or_keys():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_sanitized.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
