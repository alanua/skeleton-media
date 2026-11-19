"""Independent decoded-image observations from the owned MPV instance.

Counts observed image changes, NOT total decoded frames and never time * fps.
The screenshot excludes subtitles/OSD; decoded pixels exclude PNG metadata.
No playback, filters, startup, unpause, volume or mode mutation is performed.
"""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time


class MPVDecodedImageEvidence:
    def __init__(self, *, context, command, root, clock=time.monotonic):
        self.context = context
        self.command = command
        self.root = Path(root)
        self.clock = clock
        self._lock = threading.Lock()
        self._key = self._pixels = None
        self._changes = 0

    def capture(self):
        if not self._lock.acquire(blocking=False):
            return None
        try:
            started = self.clock()
            before = self.context()
            if (not isinstance(before, dict) or before.get('mode') != 'mpv'
                    or before.get('seeking') is not False
                    or type(before.get('revision')) is not int
                    or not isinstance(before.get('process'), tuple)
                    or len(before['process']) != 3 or not before.get('path')):
                return None
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
                return None
            with tempfile.TemporaryDirectory(prefix='frame-', dir=self.root) as directory:
                path = Path(directory)/'decoded.png'
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                result = self.command(['screenshot-to-file', str(path), 'video'], timeout=.8)
                if not isinstance(result, dict) or result.get('error') != 'success':
                    return None
                stat = path.stat()
                if stat.st_uid != os.getuid() or stat.st_nlink != 1 or not 32 <= stat.st_size <= 32_000_000:
                    return None
                from PIL import Image
                with Image.open(path) as image:
                    if image.format != 'PNG' or not 1 <= image.width * image.height <= 16_000_000:
                        return None
                    dimensions = (image.width, image.height)
                    pixels = image.convert('RGBA').tobytes()
                digest = hashlib.sha256(repr(dimensions).encode() + pixels).digest()
            after = self.context()
            if self.clock() - started > 3 or before != after:
                return None
            key = (before['process'], before['path'], before['revision'])
            if key != self._key:
                changes = 0
            else:
                changes = self._changes + int(digest != self._pixels)
            self._key, self._pixels, self._changes = key, digest, changes
            return {'process': before['process'], 'path': before['path'],
                    'observed_at': self.clock(), 'frames': changes,
                    'evidence_kind': 'decoded_image_changes',
                    'counter_is_total_decoded_frames': False}
        except Exception:
            # Exceptions can include private stream paths; absence is not success.
            return None
        finally:
            self._lock.release()
