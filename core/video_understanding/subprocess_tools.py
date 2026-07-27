from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from core.video_understanding.models import VideoUnderstandingError
from core.video_understanding.runtime_config import VideoRuntimeConfig


@dataclass(frozen=True)
class CommandRequest:
    executable_key: str
    args: tuple[str, ...]
    cwd: Path
    timeout_seconds: float | None = None
    max_output_bytes: int | None = None
    stdin: bytes | None = None


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float

    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class BoundedCommandError(VideoUnderstandingError):
    pass


class BoundedCommandRunner:
    def __init__(
        self,
        config: VideoRuntimeConfig,
        *,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._popen_factory = popen_factory
        self._monotonic = monotonic

    def build_argv(self, executable_key: str, args: Sequence[str]) -> tuple[str, ...]:
        executable = self.config.executables.get(executable_key)
        if executable is None:
            raise BoundedCommandError("EXECUTABLE_NOT_ALLOWED", "executable key is not configured")
        if isinstance(args, (str, bytes)) or len(args) > 512:
            raise BoundedCommandError("INVALID_COMMAND_ARGUMENTS", "command argument list is invalid")
        normalized: list[str] = []
        for value in args:
            if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
                raise BoundedCommandError("INVALID_COMMAND_ARGUMENT", "command argument is invalid")
            normalized.append(value)
        return (executable, *normalized)

    def run(self, request: CommandRequest) -> CommandResult:
        argv = self.build_argv(request.executable_key, request.args)
        cwd = Path(request.cwd).expanduser().resolve(strict=True)
        allowed_roots = (self.config.temp_root, self.config.artifact_root)
        if not any(cwd == root or root in cwd.parents for root in allowed_roots):
            raise BoundedCommandError("COMMAND_CWD_OUTSIDE_RUNTIME", "command cwd is outside runtime roots")
        timeout = float(request.timeout_seconds or self.config.limits.subprocess_timeout_seconds)
        output_limit = int(request.max_output_bytes or self.config.limits.subprocess_output_bytes)
        if not 0.1 <= timeout <= self.config.limits.subprocess_timeout_seconds:
            raise BoundedCommandError("INVALID_COMMAND_TIMEOUT", "command timeout is invalid")
        if not 1024 <= output_limit <= self.config.limits.subprocess_output_bytes:
            raise BoundedCommandError("INVALID_COMMAND_OUTPUT_LIMIT", "command output limit is invalid")
        if request.stdin is not None and len(request.stdin) > output_limit:
            raise BoundedCommandError("COMMAND_INPUT_TOO_LARGE", "command input exceeded limit")

        started = self._monotonic()
        try:
            process = self._popen_factory(
                list(argv),
                cwd=str(cwd),
                stdin=subprocess.PIPE if request.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                env=_safe_environment(),
            )
        except (OSError, ValueError) as exc:
            raise BoundedCommandError("COMMAND_START_FAILED", "bounded command could not start") from exc

        if request.stdin is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(request.stdin)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        stdout, stderr = self._collect(process, timeout, output_limit, started)
        duration = max(0.0, self._monotonic() - started)
        return CommandResult(argv, int(process.returncode), stdout, stderr, duration)

    def require_success(self, request: CommandRequest, *, reason_code: str) -> CommandResult:
        result = self.run(request)
        if result.returncode != 0:
            raise BoundedCommandError(reason_code, "bounded command returned non-zero status")
        return result

    def _collect(
        self,
        process: subprocess.Popen[bytes],
        timeout: float,
        output_limit: int,
        started: float,
    ) -> tuple[bytes, bytes]:
        if process.stdout is None or process.stderr is None:
            self._terminate_owned(process)
            raise BoundedCommandError("COMMAND_PIPE_UNAVAILABLE", "bounded command pipes are unavailable")
        streams = {process.stdout.fileno(): (process.stdout, bytearray()), process.stderr.fileno(): (process.stderr, bytearray())}
        selector = selectors.DefaultSelector()
        try:
            for fd, (stream, _) in streams.items():
                os.set_blocking(fd, False)
                selector.register(stream, selectors.EVENT_READ, fd)
            total = 0
            while selector.get_map() or process.poll() is None:
                elapsed = self._monotonic() - started
                if elapsed > timeout:
                    self._terminate_owned(process)
                    raise BoundedCommandError("COMMAND_TIMEOUT", "bounded command timed out")
                events = selector.select(min(0.1, max(0.0, timeout - elapsed)))
                if not events and process.poll() is not None:
                    for key in list(selector.get_map().values()):
                        total += self._read_ready(selector, key.fileobj, key.data, streams)
                        if total > output_limit:
                            self._terminate_owned(process)
                            raise BoundedCommandError("COMMAND_OUTPUT_TOO_LARGE", "bounded command output exceeded limit")
                    break
                for key, _ in events:
                    chunk = self._read_ready(selector, key.fileobj, key.data, streams)
                    total += chunk
                    if total > output_limit:
                        self._terminate_owned(process)
                        raise BoundedCommandError("COMMAND_OUTPUT_TOO_LARGE", "bounded command output exceeded limit")
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            self._terminate_owned(process)
            raise BoundedCommandError("COMMAND_STOP_FAILED", "bounded command did not stop") from exc
        finally:
            selector.close()
        return bytes(streams[process.stdout.fileno()][1]), bytes(streams[process.stderr.fileno()][1])

    @staticmethod
    def _read_ready(
        selector: selectors.BaseSelector,
        stream: object,
        fd: int,
        streams: Mapping[int, tuple[object, bytearray]],
    ) -> int:
        try:
            chunk = os.read(fd, 65_536)
        except BlockingIOError:
            return 0
        if not chunk:
            try:
                selector.unregister(stream)
            except KeyError:
                pass
            return 0
        streams[fd][1].extend(chunk)
        return len(chunk)

    @staticmethod
    def _terminate_owned(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _safe_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "NO_COLOR": "1",
    }
