import subprocess, tempfile
from pathlib import Path
import importlib.util,sys
import pytest
from patch_atlas_tv_owner import FUNCTIONS, BROWSER
@pytest.fixture
def harness(tmp_path):
    state=tmp_path/'state';state.mkdir()
    current=state/'current';current.write_text('off\n')
    browser=tmp_path/'browser';browser.write_text(BROWSER);browser.chmod(0o700)
    script=tmp_path/'harness.sh'
    setup=f'''set -Eeuo pipefail
STATE_DIR={state}
CURRENT_FILE={current}
RUNTIME_DIR={tmp_path}
HOME={tmp_path}
log() {{ :; }}
backend_unit_file() {{ printf '%s/backend-%s.unit' "$STATE_DIR" "$1"; }}
'''
    script.write_text(setup+FUNCTIONS+f'''
ATLAS_BROWSER={browser}
atlas_guard() {{ [[ "\${{GUARD:-1}}" == 1 && "$1" == off ]]; }}
launch_tty2() {{ printf 'launch\\n' >> "$STATE_DIR/calls"; printf '%s\\n' home-edge-media-backend-homeedgeatlasbrowser-123.service > "$(backend_unit_file "$ATLAS_KEY")"; }}
atlas_foreground() {{ [[ "\${{FOREGROUND:-1}}" == 1 ]]; }}
systemctl() {{
  if [[ "$*" == *"is-active"* ]]; then return 3; fi
  printf 'stop\\n' >> "$STATE_DIR/calls"
}}
sleep() {{ :; }}
"$@"
'''.replace('\\$','$'))
    def run(*args,**env):
        import os
        return subprocess.run(['bash',str(script),*args],env={**os.environ,**env},capture_output=True,text=True)
    return state,current,run
def test_launch_stop_restore_only_owned(harness):
    state,cur,run=harness
    assert run('atlas_start','ABC234').returncode==0
    assert cur.read_text().strip()=='atlas'
    assert run('atlas_stop').returncode==0
    assert cur.read_text().strip()=='off'
    assert (state/'calls').read_text().split()==['launch','stop']
def test_owner_refusal_no_action(harness):
    state,cur,run=harness
    assert run('atlas_start','ABC234',GUARD='0').returncode!=0
    assert cur.read_text().strip()=='off'
    assert not (state/'calls').exists()
def test_foreground_failure_rolls_back(harness):
    state,cur,run=harness
    assert run('atlas_start','ABC234',FOREGROUND='0').returncode!=0
    assert cur.read_text().strip()=='off'
    assert not (state/'atlas-session').exists()
    assert (state/'calls').read_text().split()==['launch','stop']
def test_invalid_code_no_action(harness):
    state,cur,run=harness
    assert run('atlas_start','https://example.invalid').returncode!=0
    assert not (state/'calls').exists()
def test_wrong_unit_not_stopped(harness):
    state,cur,run=harness
    assert run('atlas_start','ABC234').returncode==0
    (state/'backend-homeedgeatlasbrowser.unit').write_text('home-edge-media-backend-chrome-123.service')
    assert run('atlas_stop').returncode!=0
    assert cur.read_text().strip()=='atlas'
    assert (state/'calls').read_text().split()==['launch']
