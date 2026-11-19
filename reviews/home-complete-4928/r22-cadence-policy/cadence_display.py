"""Read-only Mutter display capability adapter for canonical 50/60 operations."""
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def parse_mutter_state(state):
    serial,monitors,logical,props=state
    if len(logical)!=1:raise ValueError('one logical monitor required')
    x,y,scale,transform,primary,specs,lprops=logical[0]
    if len(specs)!=1:raise ValueError('one physical monitor required')
    spec=specs[0];connector=spec[0]
    matching=[m for m in monitors if m[0][0]==connector]
    if len(matching)!=1:raise ValueError('ambiguous physical monitor')
    mon=matching[0];current=[m for m in mon[1] if m[6].get('is-current') is True]
    if len(current)!=1:raise ValueError('current mode unavailable')
    current=current[0];width,height=current[1:3]
    candidates=[m for m in mon[1] if m[1]==width and m[2]==height]
    supported={}
    selected={}
    for hz in (50,60):
        target=min(candidates,key=lambda m:abs(float(m[3])-hz)) if candidates else None
        if target is not None and abs(float(target[3])-hz)<=.2:
            supported[str(hz)]=target[0];selected[str(hz)]=target
    old=50 if abs(float(current[3])-50)<=.2 else 60 if abs(float(current[3])-60)<=.2 else None
    # Canonical helper accepts only 50/60. Refuse a trial if it cannot restore
    # the EXACT former mode ID through that existing registered operation.
    if old is None or supported.get(str(old))!=current[0]:
        raise ValueError('former mode is not reversibly representable by registered operations')
    layout=[x,y,scale,transform,primary,specs,int(props.get('layout-mode',1))]
    return dict(identity=digest(spec),logical_count=1,physical_count=1,
        width=int(width),height=int(height),hz=float(current[3]),mode_id=current[0],
        supported=supported,layout_digest=digest(layout))


def read_mutter_state():
    from gi.repository import Gio
    bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
    return bus.call_sync('org.gnome.Mutter.DisplayConfig',
        '/org/gnome/Mutter/DisplayConfig','org.gnome.Mutter.DisplayConfig',
        'GetCurrentState',None,None,Gio.DBusCallFlags.NONE,3000,None).unpack()


def read_display():
    return parse_mutter_state(read_mutter_state())
