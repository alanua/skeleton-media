"""In-process MPV intent fence; contains no I/O, media data or credentials."""
from functools import wraps
import threading


class PlaybackIntentChanged(RuntimeError):
    pass


class PlaybackIntent:
    """User mutations invalidate prepared recovery before waiting for the wire."""
    def __init__(self):
        self._state = threading.RLock()
        self._wire = threading.Lock()
        self._revision = 0
        self._pending = 0

    def read(self):
        with self._state:
            return None if self._pending else self._revision

    def operation(self, function):
        """Invalidate as soon as play/stop/control is requested, before probes."""
        @wraps(function)
        def wrapped(*args, **kwargs):
            with self._state:
                self._revision += 1
                self._pending += 1
            try:
                return function(*args, **kwargs)
            finally:
                with self._state:
                    self._pending -= 1
        return wrapped

    def commit(self, expected_revision, apply):
        """Publish recovery records before another operator mutation is admitted.

        The callback may read this revision on the same thread. It must not send
        playback mutations or wait on another thread acquiring this gate.
        """
        if type(expected_revision) is not int or expected_revision < 0 or not callable(apply):
            raise ValueError('Invalid recovery commit')
        if not self._wire.acquire(blocking=False):
            raise PlaybackIntentChanged('Playback mutation is busy')
        try:
            with self._state:
                if self._pending or self._revision != expected_revision:
                    raise PlaybackIntentChanged('Playback intent changed')
                return apply()
        finally:
            self._wire.release()

    @staticmethod
    def is_read(parts):
        return (isinstance(parts, (list, tuple)) and len(parts) == 2
                and parts[0] in ('get_property', 'get_property_string')
                and isinstance(parts[1], str) and bool(parts[1]))

    def execute(self, parts, transmit, *, expected_revision=None):
        if self.is_read(parts):
            if expected_revision is not None:
                raise ValueError('A recovery fence applies only to mutations')
            return transmit()
        internal = expected_revision is not None
        if internal:
            if type(expected_revision) is not int or expected_revision < 0:
                raise ValueError('Invalid playback revision')
            # Recovery never waits behind an operator command.
            if not self._wire.acquire(blocking=False):
                raise PlaybackIntentChanged('Playback mutation is busy')
            try:
                with self._state:
                    if self._pending or self._revision != expected_revision:
                        raise PlaybackIntentChanged('Playback intent changed')
                    self._pending += 1
                try:
                    return transmit()
                finally:
                    with self._state:
                        self._pending -= 1
            finally:
                self._wire.release()
        # Invalidate immediately, even when another mutation owns the wire.
        with self._state:
            self._revision += 1
            self._pending += 1
        try:
            with self._wire:
                return transmit()
        finally:
            with self._state:
                self._pending -= 1
