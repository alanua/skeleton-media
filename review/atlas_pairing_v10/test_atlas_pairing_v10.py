import ast
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any
import pytest
from patch_atlas_pairing_v10 import patch_player

@pytest.fixture
def ns(tmp_path):
    s=(Path(__file__).parent/'player.py').read_text()
    names={'_atlas_owned_session','mode_status','_active_backend'}
    t=ast.parse(s); t.body=[n for n in t.body if isinstance(n,ast.FunctionDef) and n.name in names]
    mode=tmp_path/'current';mode.write_text('atlas\n')
    request=tmp_path/'requested'
    (tmp_path/'atlas-session').write_text('off\nABC234\n')
    (tmp_path/'backend-homeedgeatlasbrowser.unit').write_text('home-edge-media-backend-homeedgeatlasbrowser-123.service')
    def unexpected(*a,**k):raise AssertionError('legacy Chrome probe must not run')
    import re
    d=dict(Path=Path,re=re,subprocess=SimpleNamespace(run=lambda *a,**k:SimpleNamespace(returncode=0),SubprocessError=subprocess.SubprocessError),
           MODE_FILE=mode,MODE_REQUEST_FILE=request,_current_tv_mode=lambda:mode.read_text().strip(),
           _active_tty=lambda:'tty2',_youtube_web_media_status=unexpected,_browser_media_status=unexpected,
           Any=Any,__builtins__=__builtins__)
    exec(compile(t,'<atlas-player-ast>','exec'),d)
    return d
def test_valid_atlas_not_reclassified(ns):
    assert ns['mode_status']()['mode']=='atlas'
    assert ns['_active_backend']()==('atlas',None)
    assert ns['MODE_FILE'].read_text()=='atlas\n'
def test_lease_code_and_transition(ns):
    assert ns['_atlas_owned_session']('ABC234')
    assert not ns['_atlas_owned_session']('XYZ234')
    ns['MODE_REQUEST_FILE'].touch()
    assert ns['mode_status']()['mode']=='unknown'
def test_foreign_unit_fails_closed(ns):
    (ns['MODE_FILE'].parent/'backend-homeedgeatlasbrowser.unit').write_text('home-edge-media-backend-chrome-123.service')
    assert ns['mode_status']()['mode']=='unknown'
def test_missing_or_inactive_lease_fails_closed(ns):
    ns['subprocess'].run=lambda *a,**k:SimpleNamespace(returncode=3)
    assert ns['mode_status']()['mode']=='unknown'
    assert ns['MODE_FILE'].read_text()=='atlas\n'
