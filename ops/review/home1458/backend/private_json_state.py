"""Crash-resistant private JSON writes with a unique temporary file per writer."""
import errno
import json
import os
from pathlib import Path
import tempfile

def atomic_private_json(path, value):
    path=Path(path)
    encoded=(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'wb') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:
            try:os.fsync(directory)
            except OSError as error:
                if error.errno not in {errno.EINVAL,errno.ENOTSUP,errno.EBADF}:raise
        finally:os.close(directory)
    finally:
        try:temporary.unlink()
        except FileNotFoundError:pass
