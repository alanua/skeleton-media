"""Source-only exact patch; never contacts MPV or changes runtime."""
import hashlib
BEFORE = 'f6a9c771858e5067c3e09fef93cc8c5357b4d6edfa8ebd2e3ec98787dacfc754'
AFTER = 'c6197c05b3ebfcc5fc6b91efe7dc3326d9d7ab8804ed2a177bd36d1b40c82236'
OLD = "def command(parts: list[Any], timeout: float = 2.0) -> dict[str, Any]:\n    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n    client.settimeout(timeout)\n    try:\n        client.connect(str(SOCKET))\n        client.sendall((json.dumps({'command': parts}) + '\\n').encode())\n        data = b''\n        while b'\\n' not in data:\n            chunk = client.recv(65536)\n            if not chunk:\n                break\n            data += chunk\n        return json.loads(data.split(b'\\n', 1)[0] or b'{}')\n    finally:\n        client.close()"
NEW = "def playback_control_revision() -> int | None:\n    return _PLAYBACK_INTENT.read()\n\n\ndef command(parts: list[Any], timeout: float = 2.0, *, expected_revision: int | None = None) -> dict[str, Any]:\n    return _PLAYBACK_INTENT.execute(\n        parts, lambda: _command_wire(parts, timeout), expected_revision=expected_revision)\n\n\ndef _command_wire(parts: list[Any], timeout: float = 2.0) -> dict[str, Any]:\n    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n    client.settimeout(timeout)\n    try:\n        client.connect(str(SOCKET))\n        client.sendall((json.dumps({'command': parts}) + '\\n').encode())\n        data = b''\n        while b'\\n' not in data:\n            chunk = client.recv(65536)\n            if not chunk:\n                break\n            data += chunk\n        return json.loads(data.split(b'\\n', 1)[0] or b'{}')\n    finally:\n        client.close()"

def patch(source):
    if hashlib.sha256(source.encode()).hexdigest() == AFTER:
        return source
    if hashlib.sha256(source.encode()).hexdigest() != BEFORE:
        raise ValueError('Current player source differs; preserve it and review')
    if source.count('import threading\n') != 1 or source.count(OLD) != 1:
        raise ValueError('Reviewed source boundary differs')
    source = source.replace('import threading\n','import threading\nfrom playback_intent import PlaybackIntent\n_PLAYBACK_INTENT = PlaybackIntent()\n',1)
    source = source.replace(OLD,NEW,1)
    for name in ['control','play','stop']:
        boundary='def '+name+'('
        if source.count(boundary) != 1:
            raise ValueError('Reviewed operation boundary differs')
        source=source.replace(boundary,'@_PLAYBACK_INTENT.operation\n'+boundary,1)
    if hashlib.sha256(source.encode()).hexdigest() != AFTER:
        raise ValueError('Patched source differs from tested result')
    return source
