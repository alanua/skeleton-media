"""Pure mode-generation token shared with the incoming controller guard."""
import hashlib
from pathlib import Path

def mode_token(path):
    path=Path(path)
    before=path.stat()
    value=path.read_bytes()
    after=path.stat()
    if (before.st_ino!=after.st_ino or before.st_mtime_ns!=after.st_mtime_ns
            or before.st_size!=after.st_size or len(value)>64):
        raise ValueError('Mode changed while reading')
    return hashlib.sha256(str(before.st_ino).encode()+b':'+
        str(before.st_mtime_ns).encode()+b':'+value).hexdigest()
