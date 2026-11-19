"""Source-only exact-boundary patch for the existing mode controller.

The caller must supply a freshly admitted receiver event's video digest.
This module does not observe events, execute the controller or install a service.
"""
INCOMING_FUNCTIONS = r'''
incoming_mode_context_matches() {
    [[ -n "${INCOMING_EXPECTED_CONTEXT:-}" ]] || return 0
    [[ "$INCOMING_EXPECTED_CONTEXT" =~ ^[a-f0-9]{64}$ ]] || return 1
    [[ ! -e "$STATE_DIR/requested" ]] || return 1
    /usr/bin/python3 - "$CURRENT_FILE" "$INCOMING_EXPECTED_CONTEXT" <<'PY_CONTEXT'
import hashlib,pathlib,sys
try:
 p=pathlib.Path(sys.argv[1]);a=p.stat();raw=p.read_bytes();b=p.stat()
 valid=a.st_ino==b.st_ino and a.st_mtime_ns==b.st_mtime_ns and a.st_size==b.st_size and len(raw)<=64
 token=hashlib.sha256(str(a.st_ino).encode()+b':'+str(a.st_mtime_ns).encode()+b':'+raw).hexdigest()
except Exception:valid=False;token=''
raise SystemExit(0 if valid and token==sys.argv[2] else 1)
PY_CONTEXT
}

start_kiosk_incoming_guarded() {
    INCOMING_EXPECTED_CONTEXT="${2:-}"
    [[ "$INCOMING_EXPECTED_CONTEXT" =~ ^[a-f0-9]{64}$ ]] || return 1
    incoming_mode_context_matches || return 1
    start_kiosk_incoming "$1" || return 1
    incoming_mode_context_matches || return 1
}


hide_parked_mpv_for_incoming() {
    process_running mpv || return 0
    incoming_mode_context_matches || return 1
    /usr/bin/python3 - "$HOME/.local/lib/skeleton-cast" "$CURRENT_FILE" "$STATE_DIR/requested" "$INCOMING_EXPECTED_CONTEXT" <<'PY_PARK'
import os,pathlib,sys
sys.path.insert(0,sys.argv[1])
from incoming_youtube_context import mode_token
from mpv_browser_park import park_paused_mpv,MpvIPC
if pathlib.Path(sys.argv[3]).exists() or mode_token(sys.argv[2])!=sys.argv[4]:
 raise SystemExit(1)
park_paused_mpv(MpvIPC('/run/user/'+str(os.getuid())+'/skeleton-cast-mpv.sock'))
PY_PARK
}


focus_incoming_chrome() {
    incoming_mode_context_matches || return 1
    "$CHROME_MEDIA" window-show >/dev/null 2>&1 || return 1
    incoming_mode_context_matches || return 1
    /usr/bin/python3 - "$CHROME_MEDIA" <<'PY_FOCUS'
import runpy,sys
helper=runpy.run_path(sys.argv[1])
helper['ws_method']('Page.bringToFront',{})
PY_FOCUS
}


park_mpv_for_incoming() {
    process_running mpv || return 0
    incoming_mode_context_matches || return 1
    local kind=""
    kind="$(/usr/bin/python3 - "$HOME/.local/state/skeleton-cast/current.json" <<'PY_KIND'
import json,pathlib,re,sys
try:
 d=json.loads(pathlib.Path(sys.argv[1]).read_text())
 valid=isinstance(d,dict) and re.fullmatch('[0-9a-f]{16}',str(d.get('job_id') or '')) and type(d.get('pid')) is int and d['pid']>0
 if not valid:raise ValueError()
 print('live' if d.get('live') or d.get('backend')=='iptv' else 'vod')
except Exception:raise SystemExit(1)
PY_KIND
)" || return 1
    if [[ "$kind" == live ]]; then
        stop_mpv || return 1
    elif [[ "$kind" == vod ]]; then
        "$HOME/.local/bin/skeleton-cast-control" pause >/dev/null 2>&1 || return 1
    else
        return 1
    fi
}

incoming_youtube_matches() {
    local expected="$1" observed=""
    [[ "$expected" =~ ^[a-f0-9]{64}$ ]] || return 1
    process_running chrome || return 1
    observed="$(/usr/bin/timeout 5s "$CHROME_MEDIA" status 2>/dev/null)" || return 1
    printf '%s' "$observed" | /usr/bin/python3 -c 'import hashlib,json,re,sys
raw=sys.stdin.read(65537)
try:d=json.loads(raw) if len(raw)<=65536 else {}
except Exception:d={}
d=d if isinstance(d,dict) else {}
video=d.get("video_id")
valid=isinstance(video,str) and re.fullmatch(r"[A-Za-z0-9_-]{11}",video)
matched=valid and hashlib.sha256(video.encode()).hexdigest()==sys.argv[1]
state=d.get("player_state")
raise SystemExit(0 if matched and d.get("running") is True and d.get("pause") is False and type(state) is int and state in (1,3) else 1)' "$expected"
}

start_kiosk_incoming() {
    local expected="$1"
    incoming_youtube_matches "$expected" || { log "incoming YouTube precondition not confirmed"; return 1; }
    incoming_mode_context_matches || return 1
    exit_gnome_overview || return 1
    incoming_mode_context_matches || return 1
    park_mpv_for_incoming || return 1
    incoming_mode_context_matches || return 1
    stop_games || return 1
    incoming_mode_context_matches || return 1
    hide_parked_mpv_for_incoming || return 1
    incoming_mode_context_matches || return 1
    switch_tty2 || return 1
    exit_gnome_overview || return 1
    incoming_mode_context_matches || return 1
    focus_incoming_chrome || return 1
    incoming_mode_context_matches || return 1
    sleep .8
    focus_incoming_chrome || return 1
    incoming_mode_context_matches || return 1
    incoming_youtube_matches "$expected" || { log "incoming YouTube identity changed during handoff"; return 1; }
    log "incoming YouTube foreground verified"
}

'''


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('Controller source boundary differs from reviewed baseline')
    return source.replace(old, new, 1)


def patch_controller(source):
    if 'start_kiosk_incoming()' in source:
        raise ValueError('Incoming handoff already exists; review exact source')
    source = replace_once(source, 'start_kiosk() {', INCOMING_FUNCTIONS+'start_kiosk() {')
    source = replace_once(source,
        '    kiosk) log "switch kiosk"; start_kiosk ;;',
        '    kiosk) log "switch kiosk"; start_kiosk ;;\n'
        '    kiosk-incoming) start_kiosk_incoming_guarded "${2:-}" "${3:-}" || exit $?; INCOMING_YOUTUBE_HANDOFF=1; MODE=kiosk ;;')
    source = replace_once(source,
        'if [[ "$MODE" != off ]]; then normalize_volume; ensure_hyperhdr; fi',
        'if [[ "$MODE" != off && "${INCOMING_YOUTUBE_HANDOFF:-0}" != 1 ]]; then normalize_volume; ensure_hyperhdr; fi')
    return source


def patch_wrapper(source):
    # Incoming must not replace/remove a newer user's requested mode while
    # waiting behind the canonical tv-mode.real flock.
    if 'kiosk-incoming' in source:
        raise ValueError('Incoming wrapper guard already exists')
    return replace_once(source,
        'rm -f "$REQUEST_FILE"',
        'if [[ "${1:-}" != kiosk-incoming ]]; then rm -f "$REQUEST_FILE"; fi')
