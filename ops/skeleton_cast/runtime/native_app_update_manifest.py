from __future__ import annotations

import importlib
import runpy
import sys

_TARGET = "skeleton_media.cast.native_app_update_manifest"

if __name__ == "__main__":
    runpy.run_module(_TARGET, run_name="__main__")
else:
    _impl = importlib.import_module(_TARGET)
    sys.modules[__name__] = _impl
