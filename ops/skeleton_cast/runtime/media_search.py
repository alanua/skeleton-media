from __future__ import annotations

from skeleton_media.cast import media_search as _impl

for _name, _value in vars(_impl).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__"}:
        globals()[_name] = _value

def __getattr__(name: str):
    return getattr(_impl, name)
