from __future__ import annotations

import sys
from skeleton_media.cast import trakt_sync as _impl

sys.modules[__name__] = _impl
