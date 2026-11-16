"""Source-only exact-boundary patch for the existing mode controller.

The caller must supply a freshly admitted receiver event's video digest.
This module does not observe events, execute the controller or install a service.
"""
INCOMING_FUNCTIONS = r'''incoming_youtube_matches() {
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
    exit_gnome_overview
    park_or_stop_mpv_for_browser
    stop_games
    hide_parked_mpv_for_browser
    switch_tty2
    exit_gnome_overview
    focus_chrome_wait 80 .25 || return 1
    sleep .8
    focus_chrome_wait 20 .25 || return 1
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
        '    kiosk-incoming) start_kiosk_incoming "${2:-}" || exit $?; INCOMING_YOUTUBE_HANDOFF=1; MODE=kiosk ;;')
    source = replace_once(source,
        'if [[ "$MODE" != off ]]; then normalize_volume; ensure_hyperhdr; fi',
        'if [[ "$MODE" != off && "${INCOMING_YOUTUBE_HANDOFF:-0}" != 1 ]]; then normalize_volume; ensure_hyperhdr; fi')
    return source


def patch_wrapper(source):
    if 'kiosk-incoming)' in source:
        raise ValueError('Incoming wrapper route already exists')
    return replace_once(source,
        '  chrome|kiosk|mpv|games|off) requested="${1:-}" ;;',
        '  chrome|kiosk|mpv|games|off) requested="${1:-}" ;;\n  kiosk-incoming) requested=kiosk ;;')
