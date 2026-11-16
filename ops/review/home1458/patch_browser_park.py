"""Exact-boundary patch of the isolated, currently deployed mode controller."""
from pathlib import Path


def patch(path):
    p=Path(path);s=p.read_text()
    helper='''hide_parked_mpv_for_browser() {
    process_running mpv || return 0
    if ! /usr/bin/python3 "$HOME/.local/lib/skeleton-cast/mpv_browser_park.py"; then
        log "warm MPV surface park unverified; stopping only the parked player"
        stop_mpv
    fi
}

'''
    marker='start_kiosk() {'
    assert s.count(marker)==1 and 'hide_parked_mpv_for_browser()' not in s
    s=s.replace(marker,helper+marker)
    start=s.index(marker);end=s.index('\n}',start)+2;function=s[start:end]
    boundary='    switch_tty2\n    exit_gnome_overview'
    assert function.count(boundary)==1
    function=function.replace(boundary,'    hide_parked_mpv_for_browser\n'+boundary)
    s=s[:start]+function+s[end:]
    start=s.index('start_chrome() {');end=s.index('\n}',start)+2;function=s[start:end]
    assert function.count('    switch_tty2')==1
    function=function.replace('    switch_tty2','    hide_parked_mpv_for_browser\n    switch_tty2')
    p.write_text(s[:start]+function+s[end:])


if __name__=='__main__':
    import sys
    patch(sys.argv[1])
