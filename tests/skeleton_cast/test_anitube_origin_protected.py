from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
RESOLVER = ROOT / "ops/skeleton_cast/runtime/resolver.py"
APP = ROOT / "ops/skeleton_cast/runtime/app.py"

def test_runtime_sources_parse_and_are_canonical():
    resolver = RESOLVER.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    ast.parse(resolver)
    ast.parse(app)
    assert "class OriginProtectedError" in resolver
    assert "anitube-origin-cooldown.json" in resolver
    assert "os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)" in resolver
    assert "canonical_path = parsed_page.path.rstrip(\"/\") + \".html\"" in resolver
    assert "remaining = _anitube_cooldown_remaining()" in resolver
    assert "raise OriginProtectedError" in resolver
    assert "from resolver import BrowserChallengeError, OriginProtectedError, resolve_page" in app
    assert "error_type = 'origin_protected'" in app

def test_deploy_and_rollback_are_present():
    assert (ROOT / "ops/skeleton_cast/deploy.sh").stat().st_mode & 0o111
    assert (ROOT / "ops/skeleton_cast/rollback.sh").stat().st_mode & 0o111
