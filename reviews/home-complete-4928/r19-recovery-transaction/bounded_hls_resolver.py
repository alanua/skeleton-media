"""Bounded current-resolver adapter; no application import, timers or daemon.

The fixed sibling worker resolves only the existing saved origin. Raw resolver
errors/stream URLs stay inside the private subprocess protocol, never exceptions.
"""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys


class ResolveUnavailable(RuntimeError):
    pass


class BoundedResolver:
    def __init__(self, worker):
        self.worker = Path(worker).resolve(strict=True)
        if not self.worker.is_file():
            raise ValueError('resolver worker must be a file')

    def __call__(self, origin, *, timeout=12):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 12):
            raise ValueError('resolver deadline must be in (0,12]')
        if not isinstance(origin, str) or not origin.startswith(('http://', 'https://')):
            raise ResolveUnavailable('invalid saved origin')
        proc = subprocess.Popen(
            [sys.executable, '-I', str(self.worker)],
            cwd=self.worker.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            raw, _ = proc.communicate(
                json.dumps({'page_url': origin}).encode('utf-8'), timeout=timeout)
        except BaseException:
            # Resolver providers can invoke subprocesses. Kill the whole owned
            # group; terminating only the parent could leak those children.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate(timeout=1)
            raise ResolveUnavailable('resolver deadline or cancellation') from None
        if proc.returncode != 0 or len(raw) > 4*1024*1024:
            raise ResolveUnavailable('resolver failed or response too large')
        try:
            message = json.loads(raw)
            result = message['result']
            if message.get('ok') is not True or not isinstance(result, dict):
                raise ValueError()
            if not isinstance(result.get('sources'), list):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ResolveUnavailable('invalid resolver response') from None
        return result


def prepare_recovery_source(job, source, *, refresh, resolver, timeout=12):
    """Prepare the detached transaction job using the exact reviewed selector.

    refresh is the current app's _refresh_stale_source, with the optional
    resolver injection patch. Its normal retry behavior stays unchanged.
    """
    return refresh(job, source, persist=False,
                   resolver=lambda origin: resolver(origin, timeout=timeout))
