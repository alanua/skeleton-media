"""Source-only integration correction for Atlas ownership and pairing."""
import hashlib
PLAYER_BASE="50752d8500a59ed40033b61c72c856e5aaa519e1251c0de76c1d25c1f0557099"
def once(s,a,b):
    if s.count(a)!=1:raise ValueError("ATLAS_ANCHOR "+a[:80])
    return s.replace(a,b,1)
def patch_player(s):
    if hashlib.sha256(s.encode()).hexdigest()!=PLAYER_BASE:raise ValueError("PLAYER_BASE_MISMATCH")
    # An Atlas raw state must never be reclassified from a warm Chrome PID.
    # UNKNOWN is fail-closed for an absent or mismatched lease.
    s=once(s,"    raw = _current_tv_mode()\n    if raw in {'mpv','tv'}:", "    raw = _current_tv_mode()\n    if raw == 'atlas':\n        return 'atlas', None\n    if raw in {'mpv','tv'}:")
    s=once(s,"    raw = _current_tv_mode()\n    detected_backend, detected_state = _active_backend()", """    raw = _current_tv_mode()
    if raw == 'atlas':
        return {'mode': 'atlas' if tty == 'tty2' and _atlas_owned_session() else 'unknown',
                'tv_mode': raw, 'active_tty': tty, 'detected_backend': None}
    detected_backend, detected_state = _active_backend()""")
    s=once(s,"def mode_status(*, include_transition: bool = True)",'''def _atlas_owned_session(expected_code: str | None = None) -> bool:
    """Read-only session lease probe, not physical screen verification."""
    try:
        root = MODE_FILE.parent
        if _current_tv_mode() != 'atlas' or MODE_REQUEST_FILE.exists():
            return False
        prior, code = (root / 'atlas-session').read_text(encoding='ascii').splitlines()
        if prior not in {'off', 'mpv'} or not re.fullmatch(r'[A-Z2-9]{6}', code):
            return False
        if expected_code is not None and code != expected_code:
            return False
        unit = (root / 'backend-homeedgeatlasbrowser.unit').read_text(encoding='ascii').strip()
        if not re.fullmatch(r'home-edge-media-backend-homeedgeatlasbrowser-[0-9]+\\.service', unit):
            return False
        r = subprocess.run(['systemctl','--user','is-active','--quiet',unit],
                           capture_output=True,timeout=2,check=False)
        return r.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def mode_status(*, include_transition: bool = True)''')
    compile(s,'<atlas-player-review>','exec')
    return s
def patch_adapter(s):
    s=once(s,"poster_safe,presentation_ready=lambda:False):","poster_safe,presentation_ready=lambda:False,atlas_owned=lambda code:False):")
    s=once(s,"    def tid(row):","""    def clear_for_session(code):
        # A paired screen may continue only under its exact dedicated lease.
        # A foreign Atlas session, YouTube, or an unknown owner stays blocked.
        if not atlas_owned(code):
            clear()
    def tid(row):""")
    s=once(s,"return jsonify(store.join(v['code'],require_clear=clear))","return jsonify(store.join(v['code'],require_clear=lambda:clear_for_session(v['code'].upper() if isinstance(v['code'],str) else '')))")
    s=once(s,"store.read(code,token(),'display',seen=False);clear()","store.read(code,token(),'display',seen=False);clear_for_session(code)")
    compile(s,'<atlas-adapter-review>','exec')
    return s
def patch_binding(s):
    s=once(s,"o.get('reason')=='player_running_false'","o.get('reason')=='player_runtime_not_running'")
    s=once(s,"display_clear=_atlas_live_display_clear, static=STATIC/'movie_atlas', poster_safe=_atlas_live_poster_safe)","display_clear=_atlas_live_display_clear, static=STATIC/'movie_atlas', poster_safe=_atlas_live_poster_safe, atlas_owned=player._atlas_owned_session)")
    compile(s,'<atlas-binding-review>','exec')
    return s
def patch_js(s):
    return once(s,"  } catch {\n    showError('Не вдалося відновити підключення цієї вкладки.');\n  }\n})();","""  } catch {
    showError('Не вдалося відновити підключення цієї вкладки.');
  }
  // Only the fixed local display launcher supplies a pair-code fragment.
  // The display role joins using its existing read-only capability.
  if (!controller) {
    const launchCode = location.hash.slice(1);
    if (/^[A-Z2-9]{6}$/.test(launchCode)) {
      history.replaceState(null, '', location.pathname + location.search);
      if (!sync || sync.capability.code !== launchCode) {
        if (sync) forget();
        pair(() => api.join(launchCode));
      }
    }
  }
})();""")
