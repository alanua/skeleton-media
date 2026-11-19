"""Validate optional saved VOD state before the existing boot capture writes it."""
import json
import math


def load_last_vod(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,UnicodeError,ValueError):
        return None
    if not isinstance(value,dict):
        return None
    if any(not isinstance(value.get(k),str) or not value[k].strip()
           for k in ('job_id','source_id')):
        return None
    position=value.get('position_seconds',0)
    if isinstance(position,bool):
        return None
    try:
        position=float(position)
    except (TypeError,ValueError,OverflowError):
        return None
    if not math.isfinite(position) or position<0:
        return None
    return value
