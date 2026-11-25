"""Exact-source review overlay for the existing TV mode owner, not a second executor."""
import hashlib
BASE="ed51be7311e071fabcb46af084f773f8851858eed1039c5224ec4a13b1ef3aa9"
OWNER_BASE="0932017b132bf22878b345e92dd1b6bedff8fb23de3498b0c70634dd3bc5e0d0"
FUNCTIONS=r'''
# Atlas is a foreground owner under the existing tv-mode flock.
# Never repurpose the YouTube Chrome profile or launch over a live/unknown owner.
ATLAS_STATE="$STATE_DIR/atlas-session"
ATLAS_BROWSER="$HOME/.local/bin/home-edge-atlas-browser"
ATLAS_KEY=homeedgeatlasbrowser
atlas_guard() {
    local prior="$1" report rc=0
    [[ "$prior" == off || "$prior" == mpv ]] || return 1
    [[ ! -e "$STATE_DIR/requested" ]] || return 1
    [[ "$(cat "$CURRENT_FILE" 2>/dev/null)" == "$prior" ]] || return 1
    [[ "$(cat /sys/class/tty/tty0/active 2>/dev/null)" == tty2 ]] || return 1
    # A warm hidden Chrome, active game, or MPV process is not proven clear.
    if process_running chrome || process_running mpv || process_running steam; then return 1; fi
    report="$(/usr/bin/timeout 8s "$HOME/.local/bin/home-edge-media-display-owner" status)" || rc=$?
    [[ "$rc" == 1 ]] || return 1
    printf '%s' "$report" | /usr/bin/python3 -c '
import json,sys
try:
 d=json.load(sys.stdin); prior=sys.argv[1]
 assert d["schema_version"]=="skeleton.home_edge.media_display_ownership.v1" and d["state"]=="CLEAR"
 assert any(o.get("source")=="skeleton_cast_mode" and o.get("reason")=="mode_"+prior for o in d["observations"])
 assert (prior=="off" and d["reason"]=="display_mode_off") or (prior=="mpv" and d["reason"]=="confirmed_video_not_playing" and any(o.get("source")=="skeleton_cast_player" and o.get("reason")=="player_runtime_not_running" and o.get("state")=="CLEAR" for o in d["observations"]))
except Exception:raise SystemExit(1)
' "$prior"
}
atlas_foreground() {
    local auth window
    auth="$(find "$RUNTIME_DIR" -maxdepth 1 -type f -name '.mutter-Xwaylandauth.*' -print -quit)"
    [[ -n "$auth" ]] || return 1
    window="$(DISPLAY=:0 XAUTHORITY="$auth" /usr/bin/xdotool getactivewindow 2>/dev/null)" || return 1
    [[ "$window" =~ ^[0-9]+$ ]] || return 1
    DISPLAY=:0 XAUTHORITY="$auth" /usr/bin/xprop -id "$window" WM_CLASS _NET_WM_STATE 2>/dev/null | /usr/bin/python3 -c '
import sys,re
s=sys.stdin.read()
raise SystemExit(0 if re.search(r"WM_CLASS.*\"SkeletonAtlas\"",s,re.I) and "_NET_WM_STATE_FULLSCREEN" in s else 1)
'
}
atlas_stop_owned() {
    local file unit
    file="$(backend_unit_file "$ATLAS_KEY")"
    if [[ -e "$file" ]]; then
        unit="$(cat "$file")"
        [[ "$unit" =~ ^home-edge-media-backend-homeedgeatlasbrowser-[0-9]+\.service$ ]] || return 1
        systemctl --user stop "$unit" >/dev/null 2>&1 || {
            systemctl --user is-active --quiet "$unit" && return 1
        }
        if systemctl --user is-active --quiet "$unit"; then return 1; fi
        rm -f "$file"
    fi
}
atlas_start() {
    local code="$1" prior unit file
    [[ "$code" =~ ^[A-Z2-9]{6}$ ]] || return 1
    [[ -x "$ATLAS_BROWSER" && ! -e "$ATLAS_STATE" ]] || return 1
    prior="$(cat "$CURRENT_FILE" 2>/dev/null)" || return 1
    atlas_guard "$prior" || { log "Atlas refused: display not positively clear"; return 1; }
    file="$(backend_unit_file "$ATLAS_KEY")"
    [[ ! -e "$file" ]] || return 1
    # Record the previous mode before starting the dedicated browser.
    (umask 077; printf '%s\n%s\n' "$prior" "$code" >"$ATLAS_STATE") || return 1
    if ! launch_tty2 "$ATLAS_BROWSER" "$code"; then
        if atlas_stop_owned; then rm -f "$ATLAS_STATE"; fi
        return 1
    fi
    for _ in $(seq 1 40); do
        if [[ -e "$STATE_DIR/requested" || "$(cat "$CURRENT_FILE" 2>/dev/null)" != "$prior" ]]; then break; fi
        if atlas_foreground; then
            # Foreground window proven, not physical panel or paired-session acceptance.
            printf '%s\n' atlas >"$CURRENT_FILE"
            log "Atlas dedicated fullscreen window verified"
            return 0
        fi
        sleep .25
    done
    if atlas_stop_owned; then rm -f "$ATLAS_STATE"; fi
    log "Atlas foreground unverified"
    return 1
}
atlas_stop() {
    local prior code
    [[ -r "$ATLAS_STATE" ]] || return 1
    { read -r prior; read -r code; } <"$ATLAS_STATE" || return 1
    [[ "$prior" == off || "$prior" == mpv ]] || return 1
    [[ "$code" =~ ^[A-Z2-9]{6}$ ]] || return 1
    atlas_stop_owned || return 1
    # Restore only the previous owner label. Do not start video or change audio.
    if [[ "$(cat "$CURRENT_FILE" 2>/dev/null)" == atlas ]]; then
        printf '%s\n' "$prior" >"$CURRENT_FILE"
    fi
    rm -f "$ATLAS_STATE"
    log "Atlas own browser stopped"
}
'''
BROWSER=r'''#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$#" == 1 && "$1" =~ ^[A-Z2-9]{6}$ ]] || exit 2
profile="$HOME/.local/state/skeleton/atlas-browser-profile"
umask 077
mkdir -p "$profile"
# Separate profile, no shared Chrome/YouTube session, no debugging port.
exec /usr/bin/google-chrome --ozone-platform=x11 --user-data-dir="$profile" --class=SkeletonAtlas \
  --disable-extensions --no-first-run --disable-session-crashed-bubble \
  --kiosk --start-fullscreen --window-position=0,0 --window-size=1920,1080 \
  "http://127.0.0.1:8100/movie-atlas/display#$1"
'''
def once(s,a,b):
    if s.count(a)!=1:raise ValueError("ATLAS_TV_ANCHOR_MISMATCH "+a[:80])
    return s.replace(a,b,1)
def patch_mode(s):
    if hashlib.sha256(s.encode()).hexdigest()!=BASE:raise ValueError("ATLAS_TV_BASE_MISMATCH")
    s=once(s,'MODE="\${1:-status}"'.replace('\\$','$'),FUNCTIONS+'\nMODE="\${1:-status}"'.replace('\\$','$'))
    s=once(s,'flock 9\ncase "$MODE" in','''flock 9
# These operations are serialized by the existing foreground owner.
case "$MODE" in
    atlas-start) atlas_start "\${2:-}" || exit $?; echo "TV_MODE=atlas"; exit 0 ;;
    atlas-stop) atlas_stop || exit $?; print_status; exit 0 ;;
esac
if [[ -e "$ATLAS_STATE" ]]; then
    case "$MODE" in
        restart) atlas_stop || exit $?; print_status; exit 0 ;;
        kiosk-incoming) exit 1 ;; # incoming context is not authorized for Atlas
        chrome|kiosk|mpv|mpv-ready|games|kiosk-restart|restart-kiosk|off) atlas_stop || exit $? ;;
        *) exit 2 ;;
    esac
fi
case "$MODE" in'''.replace('\\$','$'))
    return s
def patch_owner(s):
    if hashlib.sha256(s.encode()).hexdigest()!=OWNER_BASE:raise ValueError("ATLAS_OWNER_BASE_MISMATCH")
    return once(s,'MODE_FOREGROUND_OWNER = frozenset({"games", "kiosk"})','MODE_FOREGROUND_OWNER = frozenset({"games", "kiosk", "atlas"})')
